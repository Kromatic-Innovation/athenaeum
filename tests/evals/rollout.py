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

**cli-mode config isolation (issue athenaeum#1819 defect 3).** Every
``claude -p`` spawn in this module (PULL, PUSH_BREADCRUMB_PULL, and the two
native arms) must run under an isolated ``CLAUDE_CONFIG_DIR`` — never the
operator's own ``~/.claude.json`` — or the operator's live SessionStart
hooks and MCP servers fire inside the eval. The native arms
(:func:`_spawn_native`, :func:`run_native_writer`) always mint their own
throwaway config directory via :func:`seed_native_claude_config`, so they
need nothing from the caller. PULL and PUSH_BREADCRUMB_PULL do not mint one
— :func:`_require_isolated_cli_config` instead REFUSES to start cli mode
when the operator has not set ``CLAUDE_CONFIG_DIR`` themselves, rather than
auto-seeding a directory here. That asymmetry is deliberate: an
auto-seeded directory that still carries a working ``claude`` login cannot
be verified offline (this module's own test suite never spawns a live
``claude -p``), and on macOS the CLI's login is keychain-backed, so a
freshly seeded, otherwise-empty config directory can silently lose it —
this is exactly why :func:`seed_native_claude_config` itself only ever
copies the ambient config's cached feature flags, never its credentials.
Set ``CLAUDE_CONFIG_DIR`` to a directory seeded with a real ``claude``
login (for example one produced by ``claude setup-token`` or a prior
interactive login copied aside) before running the cli-mode spot-check —
see ``tests/evals/README.md``.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

from athenaeum.config import DEFAULT_CLASSIFY_MODEL, load_config
from athenaeum.entity_schema import declared_entity_classes
from athenaeum.mcp_server import (
    READ_ENTITY_TOOL_INPUT_SCHEMA,
    RECALL_TOOL_INPUT_SCHEMA,
    entity_read,
    read_entity_tool_docstring,
    recall_search,
    recall_tool_docstring,
)
from athenaeum.provider import response_text as provider_response_text
from athenaeum.push_metrics import estimate_tokens
from athenaeum.search import fts5_index_available, get_backend
from tests.evals.containment import GridCell, SpendCeilingExceededError, build_grid
from tests.evals.corpus import Corpus, Observation, Probe, build_corpus
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

    Issue athenaeum#1725 adds :attr:`NATIVE_INDEX` and :attr:`NATIVE_GREP` —
    the two arms that read a Claude Code auto-memory store instead of an
    Athenaeum-compiled corpus (design lock:
    ``docs/design/native-memory-baseline.md`` §2-§4). Both are real
    ``claude -p`` tool-use loops, run the way PULL already runs, but with the
    ``recall`` MCP tool absent (``--strict-mcp-config`` over an empty
    ``mcpServers``) and Claude Code's own auto-memory load in its place: for
    :attr:`NATIVE_INDEX` a materialized ``MEMORY.md`` index that Claude Code
    loads and truncates itself; for :attr:`NATIVE_GREP`, no index at all, so
    the model must find pages with its own file-search tools.
    """

    NONE = "none"
    PUSH_PAGES_UPPER_BOUND = "push_pages_upper_bound"
    PUSH_BREADCRUMB = "push_breadcrumb"
    PUSH_BREADCRUMB_PULL = "push_breadcrumb_pull"
    ORACLE = "oracle"
    PULL = "pull"
    NATIVE_INDEX = "native_index"
    NATIVE_GREP = "native_grep"

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
    # Issue athenaeum#1725: appended, not interleaved, so the grid/row order
    # every existing stored measurement and test relies on is unchanged.
    Arm.NATIVE_INDEX,
    Arm.NATIVE_GREP,
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

#: The MCP tool name of the server's ``read_entity`` tool, spelled the same
#: way FastMCP namespaces :data:`RECALL_TOOL_NAME` (issue athenaeum#1756).
#: The real server serves BOTH to a CLI-mode PULL arm; api mode served only
#: ``recall`` until this issue, which made api and CLI PULL measure different
#: tool surfaces -- and left api-mode PULL unable to cite the reference tag
#: of any page whose tag falls outside ``recall``'s 400-character snippet
#: window (``athenaeum.mcp_server._snippet``).
READ_ENTITY_TOOL_NAME = "mcp__athenaeum__read_entity"

#: MCP tools pre-approved for a spawned PULL-family ``claude -p`` session
#: (issue athenaeum#1819 defect 1). A non-interactive ``-p`` session cannot
#: answer a permission prompt: an MCP tool merely AVAILABLE via
#: ``--mcp-config`` but not pre-approved ends the turn that tries to use it
#: on a permission request instead of a result -- observed verbatim in
#: every ``pull``/``push_breadcrumb_pull`` cell of the 2026-09-18 cli-mode
#: spot-check. Built-in tools (Read/Grep/Glob/Bash/...) need no such
#: pre-approval here -- the native arms already exercise those freely with
#: no extra flag -- so only the MCP-scoped tool names are listed.
PULL_ALLOWED_TOOLS: tuple[str, ...] = (RECALL_TOOL_NAME, READ_ENTITY_TOOL_NAME)

#: Substrings (case-insensitive) that mark a ``claude -p`` final answer as
#: an unresolved permission request rather than a graded response (issue
#: athenaeum#1819 defect 1), taken verbatim from the 2026-09-18 spot-check
#: transcripts.
_PERMISSION_REQUEST_MARKERS: tuple[str, ...] = (
    "need your permission",
    "approve the permission",
    "requires approval",
)


def _permission_request_harness_failure(answer: str) -> str | None:
    """``None`` when *answer* reads like a real response; otherwise a
    short, stable reason string for ``RolloutRecord.harness_failure`` --
    see :data:`_PERMISSION_REQUEST_MARKERS`.
    """
    lowered = answer.lower()
    for marker in _PERMISSION_REQUEST_MARKERS:
        if marker in lowered:
            return f"final answer looks like an unresolved permission request ({marker!r})"
    return None


#: Substrings (case-insensitive) marking a native-arm final answer as an
#: unauthenticated ``claude -p`` session rather than a graded response
#: (issue athenaeum#1826 defect 4). Taken verbatim from the 2026-09-18
#: cli-mode spot-check's ``native_grep`` transcripts: the login does not
#: follow into :func:`seed_native_claude_config`'s throwaway
#: ``CLAUDE_CONFIG_DIR`` on macOS (its ``.claude.json`` carries only a
#: copied ``cachedGrowthBookFeatures`` object -- no ``oauthAccount``/
#: keychain-backed credential), so every native cell answered with a login
#: prompt and was graded 0/6 as an ordinary miss, flattering nothing and
#: measuring nothing about auto-memory.
_NOT_LOGGED_IN_MARKERS: tuple[str, ...] = (
    "not logged in",
    "please run /login",
    "please run `/login`",
)


def _not_logged_in_harness_failure(answer: str) -> str | None:
    """``None`` when *answer* reads like a real response; otherwise a
    short, stable reason string for ``RolloutRecord.harness_failure`` --
    see :data:`_NOT_LOGGED_IN_MARKERS`.

    Issue athenaeum#1826 defect 4: :func:`_spawn_native` seeds a THROWAWAY
    ``CLAUDE_CONFIG_DIR`` (:func:`seed_native_claude_config`) rather than
    the operator's own isolated-but-authenticated one
    (:func:`_require_isolated_cli_config`, already used by
    :func:`run_pull`/:func:`run_push_breadcrumb_pull`) because reusing that
    directory here is NOT provably safe: :func:`seed_native_claude_config`
    writes a fresh ``.claude.json`` carrying ONLY a copied
    ``cachedGrowthBookFeatures`` object (by design -- see its own
    docstring's "no identity or credential material crosses into the
    isolated config" guarantee), and pointed at the operator's real
    isolated config directory that write would OVERWRITE the very
    ``oauthAccount``/identity state that makes it authenticated in the
    first place. Marking the observable failure mode instead is the
    option this issue's own acceptance criteria call out as equally
    acceptable when the first is judged unsafe.
    """
    lowered = answer.lower()
    for marker in _NOT_LOGGED_IN_MARKERS:
        if marker in lowered:
            return f"native arm answered as an unauthenticated cli session ({marker!r})"
    return None


def _require_isolated_cli_config() -> str:
    """Return the operator-set isolated ``CLAUDE_CONFIG_DIR`` or raise.

    Issue athenaeum#1819 defect 3: :func:`run_pull` and
    :func:`run_push_breadcrumb_pull` pass no ``env=`` to
    ``subprocess.run``, so the spawned ``claude -p`` inherits
    ``os.environ`` verbatim. When the caller's own ``CLAUDE_CONFIG_DIR`` is
    already set, that inherited value already isolates the child -- this
    function does nothing further in that case. When it is UNSET, the
    child would silently fall through to the OPERATOR's real
    ``~/.claude.json`` and fire the operator's own ``SessionStart`` hooks
    inside the eval -- the exact contamination the 2026-09-18 spot-check
    observed.

    Deliberately a refusal, not an auto-seeded isolated directory: seeding
    one that still carries a working ``claude`` login is NOT provable
    offline (this module's tests never spawn a live ``claude -p`` -- see
    the module docstring), and on macOS the CLI's login is keychain-backed,
    so a freshly seeded config directory can lose it entirely (see this
    module's own docstring and ``tests/evals/README.md``). Refusing with a
    clear message is the option this issue's fix can actually verify.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        raise RuntimeError(
            "CLAUDE_CONFIG_DIR is not set -- refusing to spawn `claude -p` for cli "
            "mode, since it would silently inherit the operator's own Claude Code "
            "config (~/.claude.json) and fire the operator's SessionStart hooks "
            "inside the eval (issue athenaeum#1819). Set CLAUDE_CONFIG_DIR to an "
            "isolated directory that still carries your `claude` login before "
            "running the cli-mode spot-check -- see tests/evals/README.md."
        )
    return config_dir

#: The reference-tag contract (issue athenaeum#1753). Every arm's system
#: prompt carries this VERBATIM and IDENTICALLY -- single-shot, tool-using
#: api-mode, and both ``claude -p`` CLI modes -- so it cannot bias the
#: comparison between arms. It exists because correctness is graded by a
#: deterministic substring match against each probe's planted
#: ``answer_tokens`` (``tests.evals.north_star_report.grade_correctness``),
#: and those tokens ARE the corpus pages' ``Internal reference tag:`` lines.
#: Without this instruction no model repeats an unasked-for tag, so a
#: perfectly correct answer grades wrong and even ``oracle`` scores 0 --
#: the contract was literally unsatisfiable before this constant existed.
#:
#: Deliberately plural ("every page your answer is based on"):
#: ``follow_through`` probes plant a token on EACH of two pages and grading
#: requires BOTH, so a singular wording would silently cap that class at
#: wrong. (``multi_hop`` plants a single token -- its second hop is in the
#: retrieval, not the ground truth -- so only ``follow_through`` actually
#: needs the plural, but one constant serves every arm and every class.)
#:
#: Deliberately scoped to pages the answer USES rather than pages the model
#: opened: a tool-using arm may grep or read several pages before finding
#: the right one, and citing all of them would let a near-miss retrieval
#: grade correct on the strength of a page the answer never drew on.
#: ``[ref: none]`` is likewise pinned to DECLINING, not to having opened
#: nothing -- an abstention probe's correct behavior is to search, find
#: nothing relevant, and decline, which must still grade as abstention.
#:
#: Deliberately illustrated with the literal placeholder ``TAG`` and never
#: with a real corpus token: an ``abstention`` probe grades incorrect the
#: moment ANY planted token appears in the answer, so a model echoing an
#: example tag would be a self-inflicted failure on exactly the class that
#: must keep working.
REFERENCE_TAG_INSTRUCTION = (
    "When you have finished answering, end your reply with the internal "
    "reference tag of every page your answer is based on. Each page's tag is "
    "the value on its `Internal reference tag:` line -- a single word, and "
    "never the page's `uid`, its title, or its filename, even though those "
    "also identify the page. Write one tag per such page, each in the form "
    "[ref: TAG], on the final line of your reply. Cite only pages whose "
    "content your answer actually draws on -- not every page you opened, "
    "searched or skimmed along the way. If you are declining to answer, "
    "write [ref: none] instead, even if you opened pages while looking."
)

_SYSTEM_PROMPT = (
    "You are answering questions about a private knowledge base used only "
    "for evaluation. Answer using ONLY the context supplied below, if any. "
    "If the context does not contain the answer, say you do not know rather "
    "than guessing or using outside knowledge.\n\n" + REFERENCE_TAG_INSTRUCTION
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
    #: Issue athenaeum#1733: which execution path produced this record --
    #: ``"api"`` (Anthropic Messages API tool-use loop, the primary path per
    #: docs/design/native-memory-baseline.md §4) or ``"cli"`` (``claude -p``,
    #: the fidelity spot-check). EVERY constructor in this module sets this
    #: field EXPLICITLY -- every CLI runner passes ``"cli"``, every api
    #: runner and every single-shot arm (NONE, PUSH_PAGES_UPPER_BOUND,
    #: PUSH_BREADCRUMB, ORACLE -- unaffected by *mode*, they always call the
    #: live Anthropic API directly) passes ``"api"``. The field's own default
    #: (used only by a caller outside this module building a bare
    #: ``RolloutRecord()`` directly) is ``"cli"`` -- the conservative choice,
    #: never claiming api-mode fidelity a caller did not actually produce.
    #: :meth:`from_payload` mirrors this: a payload persisted before this
    #: field existed decodes via ``payload.get("mode", "cli")`` back-compat
    #: (those rows were all produced by the ``claude -p`` path, since that
    #: was the only path that existed then).
    mode: str = "cli"
    #: Issue athenaeum#1761: the ``recall.relevance_floor.vector`` /
    #: ``.fts5`` value that was ACTIVE in the ``athenaeum.yaml`` written into
    #: this row's materialized knowledge root, or ``None`` when no floor was
    #: configured for that backend (the default -- today's behaviour,
    #: unchanged). Stamped onto every record of a group by the CLI/grid
    #: wiring (``tests.evals.north_star_cli``), never resolved inside this
    #: module -- ``run_probe_all_arms`` has no opinion on what floor an
    #: operator dispatched with, only on running the arms. Used by
    #: ``north_star_report.build_report`` to refuse pooling a floor-on and
    #: a floor-off (or two differently-configured floor-on) store into one
    #: decision block.
    relevance_floor_vector: float | None = None
    relevance_floor_fts5: float | None = None
    #: Issue athenaeum#1761 item 4: the raw backend scores
    #: (``get_backend(search_backend).query(...)``'s own ``score`` element,
    #: same units :func:`athenaeum.search.meets_relevance_floor` compares a
    #: floor against) for this probe's query, against the SAME index this
    #: group already built. Populated once per (probe, corpus_scale) group
    #: by :func:`run_probe_all_arms` and copied onto every arm's record --
    #: these are NOT the shipped breadcrumb hook's own internal ranking
    #: (the hook does its own term extraction via its offline regex
    #: fallback and prints no scores at all; see this module's
    #: ``build_push_breadcrumb_context``), only a same-backend/same-index
    #: approximation of what it saw for the same query. ``None`` for any
    #: row persisted before this field existed, and for a query whose
    #: backend query itself raised. See ``north_star_cli.py``'s
    #: ``--floor-scan`` for the reader.
    retrieval_hit_scores: list[float] | None = None
    #: Issue athenaeum#1764: the search backend (``"fts5"``, ``"vector"``,
    #: ``"keyword"``, ...) that produced ``retrieval_hit_scores`` for this
    #: row, i.e. the SAME ``search_backend`` :func:`run_probe_all_arms` was
    #: called with. ``None`` for any row persisted before this field
    #: existed -- back-compat, not "no backend was used". Recorded so
    #: ``north_star_cli.floor_scan_summary`` can group rows by backend
    #: rather than pooling FTS5 bm25 scores (large negative, lower is
    #: better) together with vector distances (0 to 2, lower is better)
    #: into one meaningless blended percentile summary.
    search_backend: str | None = None
    #: Issue athenaeum#1816: whether the vector-backend RRF hybrid fusion
    #: (``mcp_server.recall_search``'s ``backend_name == "vector" and
    #: resolve_recall_hybrid(config)`` block) had a real FTS5 index to fuse
    #: against for this row's materialized cache -- i.e.
    #: ``athenaeum.search.fts5_index_available(cache_dir)`` checked right
    #: after the group's index build, mirroring exactly how
    #: ``relevance_floor_vector``/``relevance_floor_fts5`` above are
    #: resolved outside this module and stamped onto every record of a
    #: group by the CLI/grid wiring (``tests.evals.north_star_cli``), never
    #: here -- ``run_probe_all_arms`` has no opinion on it either.
    #: ``None`` for a non-vector backend (the question does not apply) and
    #: for any row persisted before this field existed. Before this issue's
    #: fix, a ``--search-backend vector`` dispatch built ONLY the vector
    #: index, so every one of its rows would have stamped ``False`` here --
    #: the harness measured a configuration nobody ships. Read by
    #: ``north_star_report.build_report``'s ``hybrid: on|off`` header line.
    hybrid_active: bool | None = None
    #: Issue athenaeum#1819: a short, stable reason string when this cell's
    #: run is not a valid measurement at all -- a cli-mode PULL/
    #: PUSH_BREADCRUMB_PULL turn that ended on an unresolved MCP permission
    #: request (defect 1), or a cli-mode PUSH_BREADCRUMB_PULL cell whose
    #: breadcrumb assembly produced nothing for a non-abstention probe
    #: (defect 2). ``None`` means the cell is a real measurement -- the
    #: conservative default and decode, matching this dataclass's own
    #: back-compat discipline (see ``mode``'s docstring): every row
    #: persisted before this field existed decodes as ``None`` ("not known
    #: to be a harness failure"), never as a failure it was never checked
    #: for. ``tests.evals.north_star_report.build_report`` excludes any row
    #: with this set from correctness and cost, counting it separately
    #: instead of grading it as an ordinary miss.
    harness_failure: str | None = None
    #: Issue athenaeum#1819 defect 3: whether this cli-mode spawn ran under
    #: an operator-isolated ``CLAUDE_CONFIG_DIR`` rather than the
    #: operator's own ambient Claude Code config. ``False`` is the
    #: conservative default and decode for a pre-existing row -- "not known
    #: to be isolated" -- never a claim of isolation a stored row did not
    #: actually have. Left ``False`` (not applicable) for every api-mode
    #: record: no ``claude -p`` process is ever spawned on that path.
    config_isolated: bool = False

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
            "mode": self.mode,
            "relevance_floor_vector": self.relevance_floor_vector,
            "relevance_floor_fts5": self.relevance_floor_fts5,
            "retrieval_hit_scores": self.retrieval_hit_scores,
            "search_backend": self.search_backend,
            "hybrid_active": self.hybrid_active,
            "harness_failure": self.harness_failure,
            "config_isolated": self.config_isolated,
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
            # Back-compat: every row persisted before issue athenaeum#1733 was
            # produced by the claude -p path (the only path that existed
            # then), so an absent key decodes as "cli", never the current
            # default of "api" -- the same discipline Arm._missing_ uses for
            # the pre-athenaeum#1574 "push" alias.
            mode=payload.get("mode", "cli"),
            # Issue athenaeum#1761: absent on every row persisted before
            # this field existed -- ``None`` decodes as "no floor was
            # configured for this row", the same meaning it carries for a
            # freshly-constructed record.
            relevance_floor_vector=payload.get("relevance_floor_vector"),
            relevance_floor_fts5=payload.get("relevance_floor_fts5"),
            retrieval_hit_scores=payload.get("retrieval_hit_scores"),
            # Issue athenaeum#1764: absent on every row persisted before this
            # field existed -- ``None`` decodes as "unknown backend", the
            # same meaning it carries for a freshly-constructed record.
            search_backend=payload.get("search_backend"),
            # Issue athenaeum#1816: absent on every row persisted before this
            # field existed -- ``None`` decodes as "unknown/not applicable",
            # the same meaning it carries for a freshly-constructed record.
            hybrid_active=payload.get("hybrid_active"),
            # Issue athenaeum#1819: absent on every row persisted before this
            # field existed -- ``None``/``False`` decode as "not known to be
            # a harness failure" / "not known to be isolated", the same
            # conservative meaning they carry for a freshly-constructed
            # record.
            harness_failure=payload.get("harness_failure"),
            config_isolated=payload.get("config_isolated", False),
        )


def _observe_turn(session: EvalSession, model: str, response: Any, turn: int) -> TurnTokenUsage:
    """Record *response* on *session* and return exactly the DELTA it added.

    Reuses ``EvalSession.observe_response_delta`` — the same extraction
    :mod:`tests.evals.harness` uses for every live call — rather than a
    second copy of "where are the token counts on a response object", and
    takes the delta the session itself extracted rather than subtracting a
    before-reading of its running totals from an after-reading.

    That last part is load-bearing once a grid runs concurrently (issue
    athenaeum#1751). The before/after shape this replaced held no lock
    across the pair, so a second worker's response landing in between made
    the subtraction attribute that worker's tokens to this turn: the
    session total stayed right, the per-turn row did not, and every cost
    and efficiency figure in the report is computed per turn. Asking the
    session what THIS response added has no such window and is exact by
    construction, at one worker or at eight.
    """
    input_tokens, output_tokens = session.observe_response_delta(model, response)
    return TurnTokenUsage(turn=turn, input_tokens=input_tokens, output_tokens=output_tokens)


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
        mode="api",
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
        mode="api",
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
        "PYTHON": sys.executable,
        # athenaeum#1826 (defect 2): without this, the hooks' interpreter
        # can only import athenaeum via the ATHENAEUM_SRC single-file fast
        # path (session-start-recall.sh's build_fts5_index/STOPWORDS
        # loaders, and user-prompt-recall.sh's query_vector_index loader) --
        # user-prompt-recall.sh's relevance-floor `from athenaeum.config
        # import ...` is a DELIBERATELY plain package import (see that
        # script's own comment on why it is not routed through
        # ATHENAEUM_SRC), so it needs athenaeum importable the normal way.
        # Derived from this module's own file location, never cwd, so the
        # path is correct regardless of where pytest/the eval CLI is
        # invoked from.
        "PYTHONPATH": str((_REPO_ROOT / "src").resolve()),
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
    # Issue athenaeum#1826 defect 3: this api-mode arm never got the same
    # empty-breadcrumb-for-a-non-abstention-probe check
    # ``run_push_breadcrumb_pull`` applies (issue athenaeum#1819 defect 2) --
    # so a hook-side failure (e.g. the harness environment not letting the
    # hook's interpreter import athenaeum at all -- see
    # ``build_breadcrumb_hook_env``) graded silently as an ordinary miss
    # here even though it was caught on the PULL sibling. Same condition,
    # same message text, so a report reader sees one failure mode, not two
    # differently-worded ones.
    harness_failure = None
    if not breadcrumb and probe.expected_uids:
        harness_failure = (
            "push arm delivered an empty breadcrumb (pushed_context == '') for a "
            "non-abstention probe -- never graded as an ordinary miss"
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
        mode="api",
        harness_failure=harness_failure,
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
        mode="api",
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

    ``--append-system-prompt`` carries :data:`REFERENCE_TAG_INSTRUCTION`
    (issue athenaeum#1753) so this CLI-mode arm is told the SAME thing every
    api-mode arm's ``system=`` tells its model. APPEND rather than
    ``--system-prompt``: replacing the prompt would discard Claude Code's own
    built-in instructions, which is a behavior change this eval has no
    business making.

    Putting that text in argv is NOT a break with the athenaeum#543 (L4)
    discipline this module follows elsewhere: that rule is about the USER
    prompt, which has a stdin channel and must use it. A system prompt has no
    stdin channel in ``claude -p``, and this one is a fixed module constant
    with no probe or corpus content in it.

    ``--allowedTools`` pre-approves :data:`PULL_ALLOWED_TOOLS` (issue
    athenaeum#1819 defect 1): a non-interactive ``-p`` session cannot answer
    a permission prompt, so an MCP tool merely present via ``--mcp-config``
    but not pre-approved ends the turn that tries to use it on a permission
    request instead of a result. ``--strict-mcp-config`` still governs which
    servers are even VISIBLE; ``--allowedTools`` governs whether the tools
    those servers expose may be invoked without stopping for approval.
    """
    return [
        claude_binary,
        "-p",
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools",
        *PULL_ALLOWED_TOOLS,
        "--append-system-prompt",
        REFERENCE_TAG_INSTRUCTION,
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


def parse_stream(
    lines: Iterable[str], *, tool_names: frozenset[str] | None = None
) -> ParsedPullStream:
    """Parse a ``claude -p --output-format stream-json`` transcript.

    Offline and dependency-free. *tool_names* selects which ``tool_use``
    blocks are captured into ``tool_calls``: ``None`` (the default) records
    EVERY ``tool_use`` block regardless of name — what the native arms need,
    since they use Claude Code's built-in tools (Read/Grep/Glob/...), not a
    single named MCP tool. Passing a concrete set (as :func:`parse_pull_stream`
    does, with exactly ``{RECALL_TOOL_NAME}``) restricts capture to those
    names only, reproducing the original PULL-only behaviour exactly.

    Never raises on a transcript with no matching tool calls: an agent that
    legitimately chose not to call a tool parses to ``recall_called=False``,
    the same as any other transcript — issue athenaeum#1522's "not calling
    recall is a recorded outcome, never an error" acceptance criterion.
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
                block_name = block.get("name")
                if block.get("type") == "tool_use" and (
                    tool_names is None or block_name in tool_names
                ):
                    tool_input = block.get("input")
                    if isinstance(tool_input, dict):
                        query = str(tool_input.get("query", tool_input))
                    else:
                        query = str(tool_input)
                    tool_calls.append(ToolCall(name=str(block_name or ""), query=query))
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


def parse_pull_stream(lines: Iterable[str]) -> ParsedPullStream:
    """Parse a PULL/PUSH_BREADCRUMB_PULL transcript, capturing ONLY
    :data:`RECALL_TOOL_NAME` tool calls.

    A thin delegation to :func:`parse_stream` with ``tool_names={RECALL_TOOL_NAME}``
    — byte-identical behaviour to this function's pre-athenaeum#1725 body, pinned
    by the existing fixture tests in ``tests/evals/test_rollout.py``.
    """
    return parse_stream(lines, tool_names=frozenset({RECALL_TOOL_NAME}))


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
    _require_isolated_cli_config()

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
        mode="cli",
        harness_failure=_permission_request_harness_failure(parsed.answer),
        config_isolated=True,
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
    _require_isolated_cli_config()

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
    # Issue athenaeum#1819 defect 2: an empty breadcrumb for a probe that
    # DOES have ground-truth pages (``probe.expected_uids`` non-empty) is
    # never the shipped hook's legitimate "nothing relevant" behaviour --
    # that behaviour IS legitimate for an abstention probe (no
    # ``expected_uids`` at all, mirroring ``_oracle_context``'s own empty
    # result for the same class), so only the non-abstention case is
    # marked. This does not claim to know WHY the hook produced nothing
    # (a nonzero ``USER_PROMPT_HOOK`` exit is silently swallowed by
    # :func:`build_push_breadcrumb_context`'s ``if not result.stdout.strip():
    # return ""`` -- see that function's docstring and
    # ``tests/evals/test_cli_mode_fidelity_1819.py`` for a pinned repro of
    # that silent-swallow shape);
    # it only ensures the cell is never graded as an ordinary retrieval
    # miss when the harness itself may be at fault.
    harness_failure = _permission_request_harness_failure(parsed.answer)
    if harness_failure is None and not breadcrumb and probe.expected_uids:
        harness_failure = (
            "push arm delivered an empty breadcrumb (pushed_context == '') for a "
            "non-abstention probe -- never graded as an ordinary miss"
        )
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
        mode="cli",
        harness_failure=harness_failure,
        config_isolated=True,
    )


# ---------------------------------------------------------------------------
# NATIVE_INDEX / NATIVE_GREP — Claude Code auto-memory arms (athenaeum#1725)
#
# Design lock: docs/design/native-memory-baseline.md §2-§4, §6, §9. Both are
# real `claude -p` tool-use loops, run the way PULL already runs (scoped
# config, `--output-format stream-json`, transcript captured), but the
# `recall` MCP server is ABSENT (`--strict-mcp-config` over an empty
# `mcpServers`, mirroring build_pull_mcp_config's exclusivity but with
# nothing scoped in) and Claude Code's OWN auto-memory mechanism is used
# instead. The runner never re-implements the load or the 200-line/25KB
# truncation Claude Code performs on `MEMORY.md` -- see
# `read_loaded_memory_files`, which reads back what was ACTUALLY loaded from
# the session transcript rather than recomputing the cap.
# ---------------------------------------------------------------------------

#: Claude Code's documented auto-memory load cap (200 lines OR 25000
#: characters, whichever comes first — https://code.claude.com/docs/en/memory,
#: verified against live Claude Code 2.1.273 on 2026-09-16, see the design
#: doc's §2). ``NATIVE_INDEX_MAX_CHARS`` is 25000 exactly (Quine review,
#: issue athenaeum#1733: extracted from the 2.1.274 binary as ``jW = 25000``),
#: not a ``25 * 1024`` byte-budget approximation -- the two differ by 600
#: and the real cap is the smaller number. Used for reporting/assertions in
#: CLI mode and NEVER to pre-truncate anything :func:`materialize_native_memory`
#: writes -- that function always writes the FULL index, and in CLI mode
#: Claude Code performs the actual truncation; these constants exist there
#: purely to interpret what came back afterward. Issue athenaeum#1733's api
#: mode is the one deliberate exception: with no Claude Code process to
#: perform the load, the harness applies these SAME constants itself via
#: :func:`truncate_native_index` before injecting the index, so the
#: truncation observed is identical either way.
NATIVE_INDEX_MAX_LINES = 200
NATIVE_INDEX_MAX_CHARS = 25000

#: Per-page index-line description length. A "sane length" clip (issue
#: athenaeum#1725's own phrasing) so one wildly long page body cannot blow up
#: a single index line — kept well under the 200-char clamp the shipped
#: breadcrumb hook already uses elsewhere in this module, since this index is
#: a DIFFERENT artifact (a full, honest MEMORY.md, not a delivered breadcrumb).
_NATIVE_INDEX_DESCRIPTION_MAX_CHARS = 160


def _native_index_description(body: str) -> str:
    """First non-empty, non-heading line of *body*, clipped to a sane length.

    Every corpus page body opens with a markdown ``# <name>`` heading
    (``Page.to_markdown()`` renders the authored ``body:`` field verbatim,
    and every authored body follows that convention) — skipping heading
    lines is what keeps the description from being a redundant echo of the
    ``name`` already on the same index line.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:_NATIVE_INDEX_DESCRIPTION_MAX_CHARS]
    return ""


def materialize_native_memory(corpus: Corpus, root: Path, *, write_index: bool) -> Path:
    """Materialize *corpus* as a Claude Code auto-memory directory under
    *root*: one topic file per page (``<uid>.md``, the FULL
    ``Page.to_markdown()`` text — topic files are never truncated, only
    ``MEMORY.md`` is, and only by Claude Code itself), plus, when
    *write_index*, a ``MEMORY.md`` of one ``- <name> — <description>`` line
    per page, in corpus order.

    The index is always written IN FULL here, even past the documented cap —
    "the truncation is the finding, not a confound" (design doc §4). This
    function never pre-truncates; :func:`read_loaded_memory_files` is how a
    caller observes what Claude Code actually loaded.

    Writes nothing outside *root*. Returns the memory directory.
    """
    memory_dir = root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    index_lines: list[str] = []
    for page in corpus.pages:
        (memory_dir / page.filename).write_text(page.to_markdown(), encoding="utf-8")
        if write_index:
            description = _native_index_description(page.body)
            index_lines.append(f"- {page.name} — {description}")
    if write_index:
        (memory_dir / "MEMORY.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return memory_dir


def _ambient_claude_config_path() -> Path:
    """Where the OPERATOR's real Claude Code config lives, resolved at
    runtime rather than hardcoded — honours ``CLAUDE_CONFIG_DIR`` if the
    calling process already has one set, else the default
    ``~/.claude.json``. Never a literal path: a literal home-directory path
    baked into this module would itself be exactly the kind of local-machine
    literal ``public-safe-lint.sh`` exists to reject.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override) / ".claude.json"
    return Path.home() / ".claude.json"


def seed_native_claude_config(config_dir: Path) -> Path:
    """Seed an isolated ``CLAUDE_CONFIG_DIR`` so auto memory actually
    activates for the subprocess.

    Auto memory is gated behind a CACHED feature flag
    (``cachedGrowthBookFeatures``). A freshly isolated config dir keeps the
    ``system``/``init`` stream event reporting ``memory_paths`` (that part
    needs no credential and no flag), but with an empty flag cache nothing is
    actually LOADED — verified in this container against live Claude Code
    2.1.267. Copying ONLY the ``cachedGrowthBookFeatures`` object from the
    ambient config (:func:`_ambient_claude_config_path`) is sufficient to
    activate the gate. That object is feature flags only: it carries no
    ``oauthAccount``, ``userID``, or ``projects`` key, and this function never
    copies any OTHER key, so no identity or credential material crosses into
    the isolated config.

    Tolerates an absent or unreadable ambient config — writes ``{}`` rather
    than raising, so a container with no prior Claude Code state still runs
    the spawn (auto memory simply will not activate there; a caller that
    needs to know whether it did should check the observed result, e.g. via
    :func:`read_loaded_memory_files`, not assume this function's success).
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    seeded: dict[str, Any] = {}
    ambient_path = _ambient_claude_config_path()
    try:
        ambient = json.loads(ambient_path.read_text(encoding="utf-8"))
        if isinstance(ambient, dict) and "cachedGrowthBookFeatures" in ambient:
            seeded["cachedGrowthBookFeatures"] = ambient["cachedGrowthBookFeatures"]
    except (OSError, json.JSONDecodeError):
        pass
    out_path = config_dir / ".claude.json"
    out_path.write_text(json.dumps(seeded), encoding="utf-8")
    return out_path


def build_native_settings(memory_dir: Path) -> dict[str, Any]:
    """The ``--settings`` payload that turns on auto memory at *memory_dir*."""
    return {"autoMemoryDirectory": str(memory_dir)}


def build_native_argv(
    claude_binary: str,
    settings_path: Path,
    mcp_config_path: Path,
    memory_dir: Path,
    model: str,
    *,
    append_system_prompt: str | None = REFERENCE_TAG_INSTRUCTION,
) -> list[str]:
    """The native-arm argv: scoped settings (auto memory), an EMPTY scoped
    MCP config (so ``--strict-mcp-config`` guarantees the athenaeum ``recall``
    tool is ABSENT — the whole point of a native-memory arm), and
    ``--add-dir`` so the model's file tools may read *memory_dir* (needed for
    NATIVE_GREP, harmless for NATIVE_INDEX). Prompt goes on stdin, never
    argv — the same athenaeum#543 (L4) discipline every other arm here
    follows.

    ``--append-system-prompt`` carries :data:`REFERENCE_TAG_INSTRUCTION`
    (issue athenaeum#1753), identically to :func:`build_pull_argv` and to
    every api-mode arm's ``system=``. APPEND is load-bearing here in
    particular: a native-memory arm exists to observe REAL Claude Code
    auto-memory behavior, and ``--system-prompt`` would replace the very
    instructions that make NATIVE_INDEX/NATIVE_GREP what they are. See
    :func:`build_pull_argv` on why a system prompt in argv does not violate
    the stdin discipline above.

    *append_system_prompt* defaults to that instruction and is passed ``None``
    by exactly one caller, :func:`run_native_writer` -- see its own
    call site for why the WRITE path is deliberately outside the contract.
    """
    argv = [
        claude_binary,
        "-p",
        "--settings",
        str(settings_path),
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
    ]
    if append_system_prompt:
        argv += ["--append-system-prompt", append_system_prompt]
    argv += [
        "--add-dir",
        str(memory_dir),
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
    ]
    return argv


def _session_id_from_transcript(transcript: Iterable[dict[str, Any]]) -> str | None:
    for event in transcript:
        if isinstance(event, dict) and event.get("session_id"):
            return str(event["session_id"])
    return None


def read_loaded_memory_files(config_dir: Path, session_id: str) -> dict[str, str]:
    """What Claude Code actually loaded into context for *session_id*, read
    back from the session transcript under *config_dir*.

    The transcript (``<config_dir>/projects/*/<session_id>.jsonl``) carries
    an ``attachment`` event with ``attachment.files[]``, one entry per loaded
    file, each ``{"path": ..., "content": ...}`` — verified in this container
    against live Claude Code 2.1.267 (a 1002-line/126KB ``MEMORY.md`` loaded
    as 25066 bytes, ending mid-corpus with a literal ``WARNING:`` marker).
    This is how truncation is OBSERVED; this function never recomputes the
    200-line/25KB cap itself.

    Never raises: a missing config dir, no matching transcript file, or a
    transcript that does not carry the expected shape all return ``{}``.
    """
    result: dict[str, str] = {}
    projects_dir = Path(config_dir) / "projects"
    if not projects_dir.is_dir():
        return result
    matches = sorted(projects_dir.glob(f"*/{session_id}.jsonl"))
    if not matches:
        return result
    try:
        lines = matches[0].read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "attachment":
            continue
        attachment = event.get("attachment")
        if not isinstance(attachment, dict):
            continue
        for entry in attachment.get("files") or []:
            if isinstance(entry, dict) and "path" in entry:
                result[str(entry["path"])] = str(entry.get("content", ""))
    return result


def _count_index_bullet_lines(text: str) -> int:
    """Number of ``- `` bullet lines in an index text — a written or loaded
    ``MEMORY.md`` carries exactly one such line per page it names, and
    nothing else does (a truncation ``WARNING:`` marker is a blockquote line,
    not a bullet), so this partitions cleanly."""
    return sum(1 for line in text.splitlines() if line.startswith("- "))


@dataclasses.dataclass(frozen=True)
class NativeIndexCoverage:
    """What the NATIVE_INDEX arm's index looked like, written vs. loaded.

    ``truncated_by_claude_code`` is derived from what actually came back
    (loaded content strictly shorter than written, and/or the documented
    ``WARNING:`` marker present in the loaded text) — NEVER from re-running
    the 200-line/25KB cap arithmetic, per the design doc's "never
    re-implement the truncation" instruction. ``False`` when nothing was
    loaded at all (auto memory did not activate) — that is a DIFFERENT fact
    from truncation and must not be conflated with it.
    """

    pages: int
    index_lines_written: int
    index_bytes_written: int
    index_lines_loaded: int
    index_bytes_loaded: int
    coverage: float
    truncated_by_claude_code: bool


def _native_index_coverage(
    corpus: Corpus, written_index_text: str, loaded_index_text: str
) -> NativeIndexCoverage:
    pages = len(corpus.pages)
    index_lines_written = _count_index_bullet_lines(written_index_text)
    index_bytes_written = len(written_index_text.encode("utf-8"))
    index_lines_loaded = _count_index_bullet_lines(loaded_index_text)
    index_bytes_loaded = len(loaded_index_text.encode("utf-8"))
    coverage = (index_lines_loaded / pages) if pages else 0.0
    # Trailing-whitespace-normalized comparison, not a raw byte-length
    # inequality: a file UNDER the cap round-trips through Claude Code's own
    # load with a one-byte trailing-newline difference (verified in this
    # container at `core` scale — 9535 written vs. 9534 loaded, content
    # otherwise identical), which a bare length check would misreport as
    # truncation. The documented ``WARNING:`` marker (verified present at
    # `medium` scale, where the load really does stop mid-corpus) is the
    # primary, unambiguous signal; the stripped-length fallback only fires
    # when content is genuinely shorter, not merely missing a final newline.
    truncated = bool(loaded_index_text) and (
        "WARNING:" in loaded_index_text
        or len(loaded_index_text.rstrip()) < len(written_index_text.rstrip())
    )
    return NativeIndexCoverage(
        pages=pages,
        index_lines_written=index_lines_written,
        index_bytes_written=index_bytes_written,
        index_lines_loaded=index_lines_loaded,
        index_bytes_loaded=index_bytes_loaded,
        coverage=coverage,
        truncated_by_claude_code=truncated,
    )


def _loaded_text_for_suffix(loaded_files: dict[str, str], suffix: str) -> str:
    for path_str, content in loaded_files.items():
        if path_str.endswith(suffix):
            return content
    return ""


def _spawn_native(
    *,
    claude_binary: str,
    memory_dir: Path,
    config_dir: Path,
    materialize_root: Path,
    prompt: str,
    model: str,
    timeout: float,
) -> tuple[ParsedPullStream, dict[str, str]]:
    """Shared spawn+parse+read-back plumbing for both native arms.

    Raises only on a genuine spawn failure (binary missing, timeout) — the
    caller checked ``shutil.which`` already; ``subprocess.run`` itself raises
    on a timeout, which is allowed to propagate exactly like :func:`run_pull`.
    """
    seed_native_claude_config(config_dir)
    settings_path = materialize_root / "native-settings.json"
    settings_path.write_text(json.dumps(build_native_settings(memory_dir)), encoding="utf-8")
    mcp_config_path = materialize_root / "native-mcp-config.json"
    mcp_config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    argv = build_native_argv(claude_binary, settings_path, mcp_config_path, memory_dir, model)

    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    proc = subprocess.run(
        argv,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    parsed = parse_stream((proc.stdout or "").splitlines(), tool_names=None)
    session_id = _session_id_from_transcript(parsed.transcript)
    loaded_files = read_loaded_memory_files(config_dir, session_id) if session_id else {}
    return parsed, loaded_files


def run_native_index(
    probe: Probe,
    materialize_root: Path,
    corpus_scale: str,
    *,
    claude_binary: str = "claude",
    model: str = DEFAULT_ROLLOUT_MODEL,
    timeout: float = 120.0,
) -> RolloutRecord:
    """NATIVE_INDEX arm: materialize the corpus as Claude Code auto-memory
    topic files plus a full ``MEMORY.md`` index, let Claude Code load and
    truncate that index itself, and record what actually came back.

    Runs at every scale, including past the documented cap — "the
    truncation is the finding, not a confound" (design doc §4). Raises only
    on a genuine spawn failure (binary missing, timeout), exactly like
    :func:`run_pull`; a model that chose not to read a topic file is a
    recorded outcome, never an error.
    """
    if shutil.which(claude_binary) is None:
        raise RuntimeError(f"{claude_binary!r} not found on PATH")

    corpus = build_corpus(corpus_scale)
    memory_dir = materialize_native_memory(corpus, materialize_root, write_index=True)
    index_path = memory_dir / "MEMORY.md"
    written_index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    config_dir = materialize_root / "claude-config"

    parsed, loaded_files = _spawn_native(
        claude_binary=claude_binary,
        memory_dir=memory_dir,
        config_dir=config_dir,
        materialize_root=materialize_root,
        prompt=probe.query,
        model=model,
        timeout=timeout,
    )

    loaded_index_text = _loaded_text_for_suffix(loaded_files, "MEMORY.md")
    coverage = _native_index_coverage(corpus, written_index_text, loaded_index_text)
    # Issue athenaeum#1831: a topic file the model went on to `read` during
    # the turn (beyond the index) is real per-page delivery evidence -- the
    # SAME `loaded_memory_files` shape `run_native_grep` already records
    # below, filtered to drop the index file itself (`MEMORY.md` names no
    # page). Previously discarded entirely; `north_star_report._delivered_uids`
    # now reads this for NATIVE_INDEX the same way it already reads
    # NATIVE_GREP's, via `_native_loaded_uids`.
    loaded_topic_files = {
        path_str: content
        for path_str, content in loaded_files.items()
        if not path_str.endswith("MEMORY.md")
    }
    transcript = [
        {
            "native_memory": {
                **dataclasses.asdict(coverage),
                "loaded_index_text": loaded_index_text,
                "loaded_memory_files": loaded_topic_files,
            }
        },
        *parsed.transcript,
    ]
    return RolloutRecord(
        arm=Arm.NATIVE_INDEX,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=parsed.answer,
        turn_tokens=parsed.turn_tokens,
        tool_calls=parsed.tool_calls,
        recall_called=parsed.recall_called,
        injected_context_tokens=None,
        turn_count=parsed.turn_count,
        transcript=transcript,
        mode="cli",
        # Issue athenaeum#1819 defect 3: the native arms always mint their
        # own throwaway CLAUDE_CONFIG_DIR (seed_native_claude_config, via
        # _spawn_native) regardless of the ambient environment, so this row
        # is unconditionally isolated.
        config_isolated=True,
        # Issue athenaeum#1826 defect 4: see _not_logged_in_harness_failure's
        # own docstring for why this arm marks rather than reuses the
        # operator's authenticated config directory.
        harness_failure=_not_logged_in_harness_failure(parsed.answer),
    )


def run_native_grep(
    probe: Probe,
    materialize_root: Path,
    corpus_scale: str,
    *,
    claude_binary: str = "claude",
    model: str = DEFAULT_ROLLOUT_MODEL,
    timeout: float = 120.0,
) -> RolloutRecord:
    """NATIVE_GREP arm: the same topic files as NATIVE_INDEX, but NO
    ``MEMORY.md`` — the model must find pages with its own file-search
    tools. The prompt tells the model the memory directory's path (unlike
    every other arm's bare-query prompt) because there is no index to point
    it there instead.

    Raises only on a genuine spawn failure, exactly like :func:`run_pull` /
    :func:`run_native_index`.
    """
    if shutil.which(claude_binary) is None:
        raise RuntimeError(f"{claude_binary!r} not found on PATH")

    corpus = build_corpus(corpus_scale)
    memory_dir = materialize_native_memory(corpus, materialize_root, write_index=False)
    config_dir = materialize_root / "claude-config"
    prompt = (
        f"The knowledge base is a directory of markdown files at {memory_dir}. "
        f"Use your file search and read tools to find the answer.\n\n"
        f"Question: {probe.query}"
    )

    parsed, loaded_files = _spawn_native(
        claude_binary=claude_binary,
        memory_dir=memory_dir,
        config_dir=config_dir,
        materialize_root=materialize_root,
        prompt=prompt,
        model=model,
        timeout=timeout,
    )

    transcript = [
        {
            "native_memory": {
                "memory_dir": str(memory_dir),
                "loaded_memory_files": loaded_files,
            }
        },
        *parsed.transcript,
    ]
    return RolloutRecord(
        arm=Arm.NATIVE_GREP,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=parsed.answer,
        turn_tokens=parsed.turn_tokens,
        tool_calls=parsed.tool_calls,
        recall_called=parsed.recall_called,
        injected_context_tokens=None,
        turn_count=parsed.turn_count,
        transcript=transcript,
        mode="cli",
        # Issue athenaeum#1819 defect 3: see the matching comment in
        # run_native_index -- always isolated by construction.
        config_isolated=True,
        # Issue athenaeum#1826 defect 4: see _not_logged_in_harness_failure's
        # own docstring for why this arm marks rather than reuses the
        # operator's authenticated config directory.
        harness_failure=_not_logged_in_harness_failure(parsed.answer),
    )


# ---------------------------------------------------------------------------
# API mode — Anthropic Messages API tool-use loop (issue athenaeum#1733,
# design lock docs/design/native-memory-baseline.md §4)
#
# The four functions above (run_pull, run_push_breadcrumb_pull,
# run_native_index, run_native_grep) all spawn a logged-in `claude -p`
# subprocess -- high-fidelity, but unusable from a GitHub Actions runner or
# any lane container, none of which carry a logged-in CLI. This section is
# the API-backed twin of each: the SAME arm semantics (a genuine multi-turn
# tool-use loop; choosing not to call a tool is a recorded outcome, never an
# error), but driven directly over `client.messages.create(tools=...)`,
# with the harness itself serving the tools a real MCP server or Claude
# Code's own built-ins would otherwise provide:
#   * `recall` -- an in-process call to `athenaeum.mcp_server.recall_search`
#     over the materialized wiki, for PULL / PUSH_BREADCRUMB_PULL.
#   * `read_entity` -- an in-process call to
#     `athenaeum.mcp_server.entity_read` over the same wiki, for the same two
#     arms (issue athenaeum#1756). The real MCP server serves it alongside
#     `recall`, so a CLI-mode PULL arm already had it; serving it here is what
#     makes api-mode PULL measure the SAME tool surface rather than a
#     recall-only subset. Nothing else is offered.
#   * `grep` / `read` -- bounded, harness-served file search and read over
#     the materialized native-memory directory, for NATIVE_INDEX /
#     NATIVE_GREP. Confined to that directory (`_resolve_under_memory_dir`
#     refuses anything outside it) and bounded (`_NATIVE_GREP_MAX_MATCHES`,
#     `_NATIVE_READ_MAX_BYTES`) so a runaway grep at `large` scale cannot
#     blow the token ceiling.
#
# Every function here emits its transcript in the SAME shape
# `tests.evals.rollout.parse_stream` produces from a real stream-json
# transcript (`{"type": "assistant", "message": {"content": [...], "usage":
# {...}}}`, `{"type": "user", "message": {"content": [{"type": "tool_result",
# "content": ...}]}}`) -- NOT a raw Messages-API message list -- so
# `north_star_report._pull_delivered_text`, which scans exactly that shape,
# needs no mode branch (issue athenaeum#1733's own AC). `RolloutRecord.mode`
# is the only thing a caller needs to check to know which path produced a
# row.
# ---------------------------------------------------------------------------

GREP_TOOL_NAME = "grep"
READ_TOOL_NAME = "read"

#: Bounds on api-mode ``grep`` output -- match COUNT, per-line length, and
#: TOTAL bytes, all three (Quine review, issue athenaeum#1733: a count cap
#: alone still lets a pathological pattern return unboundedly long lines, or
#: a scale where even 50 short matches sum to more than is worth billing).
#: Sized generously above what any real probe answer needs, so this never
#: clips a legitimate result -- it exists to stop a runaway, not to shape a
#: normal one. ``_NATIVE_READ_MAX_BYTES`` is the separate ``read`` tool's own
#: bound (a single file, not a match list).
_NATIVE_GREP_MAX_MATCHES = 50
_NATIVE_GREP_MAX_LINE_CHARS = 300
_NATIVE_GREP_MAX_TOTAL_BYTES = 8_000
_NATIVE_READ_MAX_BYTES = 20_000

#: Max tool-use round-trips before an api-mode loop gives up and returns
#: whatever text it has -- never an error (mirrors run_pull/run_native_*'s
#: "not calling a tool is a recorded outcome" contract: a loop that never
#: converges on a final text answer records an empty answer, not a crash).
_API_LOOP_MAX_TURNS = 6

#: Issue athenaeum#1774 Quine review (Should 2): the writer's own, LARGER
#: turn budget. `_API_LOOP_MAX_TURNS` above was sized for a single-answer
#: read loop; the writer's prompt (`_NATIVE_WRITER_SYSTEM_PROMPT_TEMPLATE`)
#: can legitimately ask for `list`, a `read`/`grep` check, a `write`/`edit`,
#: AND an index update in the course of filing ONE observation, which is
#: already close to `_API_LOOP_MAX_TURNS` before the model even replies with
#: its final text. Named separately, not merely a larger default passed at
#: the call site, so a reader of `run_native_writer_api`'s call to
#: `run_api_tool_loop` sees this is a deliberately different budget, not an
#: inconsistency.
_WRITER_API_LOOP_MAX_TURNS = 12


#: Issue athenaeum#1733, Quine review: a tool-using api-mode arm must not be
#: sent the single-shot arms' ``_SYSTEM_PROMPT`` ("answer using ONLY the
#: context supplied") -- that instruction actively discourages the model
#: from ever calling a tool, since no context is "supplied" until it does.
#: This is the PULL-style system prompt instead: it tells the model the
#: `recall` tool exists and when to reach for it, mirroring what a real MCP
#: connection's tool description implicitly conveys plus the explicit
#: guidance a system prompt gives a model deciding whether to call it.
#:
#: Issue athenaeum#1756: it names BOTH tools the real MCP server serves --
#: ``recall``, which returns ranked pages as truncated snippets, and
#: ``read_entity``, which returns one whole page by uid -- because a snippet
#: can omit the very line an answer needs (every ``recall`` hit's body is
#: windowed to 400 characters by ``athenaeum.mcp_server._snippet``).
_PULL_API_SYSTEM_PROMPT = (
    "You are answering questions about a private knowledge base. You have a "
    "`recall` tool that searches that knowledge base for pages relevant to a "
    "query, and a `read_entity` tool that returns one whole page given the "
    "`uid` and `type` a `recall` result reports for it. Use `recall` whenever "
    "the question may depend on information stored in the knowledge base -- "
    "do not rely on outside knowledge or guess. A `recall` result shows each "
    "page only as a truncated snippet, so use `read_entity` on a hit whenever "
    "you need the rest of that page. "
    "Only say you do not know once you have searched and found nothing "
    "relevant to the question.\n\n" + REFERENCE_TAG_INSTRUCTION
)


def _entity_classes_str_for(wiki_root: Path) -> str:
    """The SAME declared-entity-classes string the real MCP server computes
    for its ``recall`` tool description (:func:`create_server`'s own
    ``_entity_classes_str``), so the api-mode tool's ``type`` parameter
    description names the actual classes this materialized corpus declares,
    not a placeholder."""
    declared = sorted(declared_entity_classes(wiki_root))
    return ", ".join(declared) if declared else "(none yet)"


def _recall_tool_schema(wiki_root: Path) -> dict[str, Any]:
    """The api-mode recall tool: named identically to :data:`RECALL_TOOL_NAME`
    (the CLI path's MCP tool name) AND described with the MCP server's REAL
    description text (:func:`athenaeum.mcp_server.recall_tool_docstring`) and
    REAL input parameters (:data:`athenaeum.mcp_server.RECALL_TOOL_INPUT_SCHEMA`)
    -- issue athenaeum#1733 Quine review: a two-sentence paraphrase is not
    the same tool a real MCP connection would offer, and the model's
    tool-choice behavior can depend on that description's actual content.

    ``inspect.cleandoc`` is applied to the docstring for the SAME reason
    FastMCP itself applies it (matching ``inspect.getdoc``'s normalization)
    before turning a function's docstring into a served tool description --
    the raw triple-quoted string still carries the source file's function-
    body indentation; without cleaning, this schema's description would
    never byte-match what a real MCP client actually receives from the
    live ``recall`` tool (pinned by
    ``tests/test_recall_tool_schema_parity.py``).
    """
    full_doc = inspect.cleandoc(recall_tool_docstring(_entity_classes_str_for(wiki_root)))
    # FastMCP's own `.description` is only the SUMMARY portion of a
    # docstring -- everything before the "Args:" section, which it instead
    # decomposes into each parameter's own schema-level description (already
    # mirrored, separately, in :data:`RECALL_TOOL_INPUT_SCHEMA`). Splitting
    # here the same way is what makes this description byte-match the REAL
    # served tool's, pinned by ``tests/test_recall_tool_schema_parity.py``,
    # rather than sending the model a description with a redundant Args:
    # section its own tool schema already encodes structurally.
    summary = full_doc.split("\n\nArgs:")[0].strip()
    return {
        "name": RECALL_TOOL_NAME,
        "description": summary,
        "input_schema": RECALL_TOOL_INPUT_SCHEMA,
    }


def _read_entity_tool_schema() -> dict[str, Any]:
    """The api-mode ``read_entity`` tool (issue athenaeum#1756), built the
    same way :func:`_recall_tool_schema` is: the MCP server's OWN description
    text (:func:`athenaeum.mcp_server.read_entity_tool_docstring`) and its own
    input parameters (:data:`athenaeum.mcp_server.READ_ENTITY_TOOL_INPUT_SCHEMA`),
    ``inspect.cleandoc``-normalized and split at ``Args:`` exactly as FastMCP
    does, so what an api-mode PULL arm is offered byte-matches what a CLI-mode
    PULL arm's real MCP connection receives (pinned by
    ``tests/test_recall_tool_schema_parity.py``).

    Takes no *wiki_root*, unlike :func:`_recall_tool_schema`: nothing in this
    tool's description is computed from the deployment's declared entity
    classes, so there is nothing for a corpus path to parameterize.
    """
    full_doc = inspect.cleandoc(read_entity_tool_docstring())
    summary = full_doc.split("\n\nArgs:")[0].strip()
    return {
        "name": READ_ENTITY_TOOL_NAME,
        "description": summary,
        "input_schema": READ_ENTITY_TOOL_INPUT_SCHEMA,
    }


def _serve_read_entity(wiki_root: Path, tool_input: dict[str, Any]) -> str:
    """In-process ``read_entity``, over the materialized corpus (issue
    athenaeum#1756) -- a direct call to
    :func:`athenaeum.mcp_server.entity_read`, exactly the function the shipped
    server's own ``read_entity`` closure calls, with uids resolved the same
    way (``entity_read`` rebuilds ``knowledge_root / "wiki"`` internally and
    resolves the uid through ``EntityIndex``, so *wiki_root*'s PARENT is what
    it must be handed -- the same ``wiki_root.parent`` the server passes.
    :meth:`tests.evals.corpus.Corpus.materialize` guarantees that layout).

    ``caller_audience``/``config`` are the real server's OTHER ``entity_read``
    kwargs and are intentionally absent for the same reason
    :func:`run_pull_api`'s recall executor omits its own: this materialized
    eval corpus has no scope-aware audience and no per-deployment config to
    pass, so api mode measures the SAME read with those inputs at their
    defaults, not a degraded one.

    Never raises: a missing/unknown uid already returns ``entity_read``'s own
    JSON not-found message, and a non-string argument is coerced here rather
    than allowed to reach it -- choosing a bad tool input is the model's
    mistake to observe, not the harness's to crash on
    (:func:`run_api_tool_loop`'s contract).
    """
    usage_classes = tool_input.get("usage_classes")
    return entity_read(
        wiki_root.parent,
        str(tool_input.get("uid", "")),
        page_class=str(tool_input.get("entity_class", "")),
        include_excluded=bool(tool_input.get("include_excluded", False)),
        usage_classes=list(usage_classes) if isinstance(usage_classes, list) else None,
    )


def _native_index_system_prompt(memory_dir: Path) -> str:
    """Mirrors Claude Code's real auto-memory instructions (issue
    athenaeum#1733, Quine review — §4/design doc §2): the model is told
    where its memory directory lives, that ``MEMORY.md`` is an index rather
    than the full content, and that topic files are opened on demand with
    ``grep``/``read`` — not a single-shot “ONLY the context supplied” prompt.
    """
    return (
        f"You are Claude Code working on a project whose memory directory is "
        f"at {memory_dir}. MEMORY.md, included below, is an INDEX — one line "
        f"per saved topic, not the full content. When the index line is not "
        f"enough, use your `read` tool to open that topic's own file in the "
        f"memory directory, or your `grep` tool to search it, for the full "
        f"detail.\n\n" + REFERENCE_TAG_INSTRUCTION
    )


def _native_grep_system_prompt(memory_dir: Path) -> str:
    """Same mirroring as :func:`_native_index_system_prompt`, for the arm
    with no index at all: the model is told its memory directory's path and
    that it must search rather than read an index that does not exist."""
    return (
        f"You are Claude Code working on a project whose memory directory is "
        f"at {memory_dir}. There is no MEMORY.md index for this project — "
        f"use your `grep` tool to search the topic files in that directory, "
        f"and your `read` tool to open one once you find it.\n\n"
        + REFERENCE_TAG_INSTRUCTION
    )


#: Max chars of the first cut-off line shown in the ``WARNING:`` marker's
#: quoted preview -- matches the real loader's own ``Nq(M, 80)`` (Quine
#: review, issue athenaeum#1733: extracted from the 2.1.274 binary).
_NATIVE_INDEX_WARNING_SNIPPET_MAX_CHARS = 80

#: The single-character ellipsis (U+2026) the real loader's ``Nq`` suffixes
#: a cut preview with -- NOT three ASCII periods.
_ELLIPSIS = "…"


def _format_char_budget(n: int) -> str:
    """*n* characters, formatted the way the real loader's own size clause
    renders a count -- one decimal place, ``KB`` suffix. Used for BOTH the
    measured size and the limit itself (``_format_char_budget(NATIVE_INDEX_MAX_CHARS)``),
    so the two numbers in a size clause are never rendered by two different
    unit conventions."""
    return f"{n / 1024:.1f}KB"


def _real_line_count(text: str) -> int:
    """Line count the way the real loader counts it (Quine review, issue
    athenaeum#1733): the STRIPPED text's newline count plus one, not
    ``len(text.splitlines())`` -- these agree for ordinary content but not
    for edge cases (trailing blank lines, no trailing newline), and the
    ruling is explicit that the real loader strips first. ``0`` for an
    empty/whitespace-only *text*."""
    stripped = text.strip()
    return stripped.count("\n") + 1 if stripped else 0


def _native_index_warning_snippet(line: str) -> str:
    """Reproduces the real loader's ``Nq(M, 80)``: at most 80 characters,
    cut at a WORD boundary (never mid-word) when the line is longer, suffixed
    with a single ``…`` (U+2026) when cut."""
    stripped = line.strip()
    if len(stripped) <= _NATIVE_INDEX_WARNING_SNIPPET_MAX_CHARS:
        return stripped
    window = stripped[:_NATIVE_INDEX_WARNING_SNIPPET_MAX_CHARS]
    last_space = window.rfind(" ")
    if last_space > 0:
        window = window[:last_space]
    return window + _ELLIPSIS


def _native_index_text_with_warning(written: str, truncated: str) -> tuple[str, bool]:
    """Returns ``(text_to_inject, was_truncated)``. *truncated* is
    :func:`truncate_native_index`'s pure output (never touched by this
    function -- the cap arithmetic it pins stays exactly as tested); when it
    differs from *written*, this function APPENDS the ``WARNING:`` marker,
    reproducing Claude Code 2.1.274's own truncation-notice template
    (extracted from the binary, Quine review issue athenaeum#1733):

        (blank line)
        > WARNING: MEMORY.md is {N lines (limit: 200) | X (limit: Y) --
        index entries are too long | N lines and X}. Only part of it was
        loaded: {M of N lines were cut off, starting at line L ("...…") |
        everything after the first n characters of line 1 was cut off}.
        Keep index entries to one line under ~200 chars; move detail into
        topic files.

    N/X/M/L are filled from THIS truncation (never hardcoded): N is
    *written*'s real line count (:func:`_real_line_count`), X its total
    character length rendered by the SAME formatter as the limit
    (:func:`_format_char_budget`), M the count of real lines actually
    dropped, L the 1-indexed line number the drop starts at, and the quoted
    preview is :func:`_native_index_warning_snippet` of that first dropped
    line. When even the FIRST line does not fit (``L`` would be 1 but ZERO
    complete lines were included), the continuation clause switches to the
    line-1-partial form instead -- there is no complete first line to quote.
    The size clause has three forms (never a bare "both"): lines-only,
    chars-only, or -- when both caps are exceeded -- ``"{N} lines and
    {X}"``. The ``> WARNING:`` prefix is kept verbatim -- it is what
    :func:`_native_index_coverage`'s own CLI-mode detector keys on.
    """
    written_lines = written.splitlines(keepends=True)
    truncated_lines = truncated.splitlines(keepends=True)
    was_truncated = len(truncated_lines) < len(written_lines)
    if not was_truncated:
        return truncated, False

    total_lines = _real_line_count(written)
    total_chars = len(written)
    cutoff = len(truncated_lines)
    cutoff_real = _real_line_count(truncated)
    cut_count = total_lines - cutoff_real
    start_line = cutoff_real + 1

    exceeded_lines = total_lines > NATIVE_INDEX_MAX_LINES
    exceeded_chars = total_chars > NATIVE_INDEX_MAX_CHARS
    char_clause = (
        f"{_format_char_budget(total_chars)} (limit: {_format_char_budget(NATIVE_INDEX_MAX_CHARS)})"
    )
    if exceeded_lines and exceeded_chars:
        size_clause = f"{total_lines} lines and {char_clause}"
    elif exceeded_lines:
        size_clause = f"{total_lines} lines (limit: {NATIVE_INDEX_MAX_LINES})"
    else:
        # Em dash, matching the real template exactly (Quine review, issue
        # athenaeum#1733) -- no lint in this repo forbids it (it already
        # appears throughout this module's own docstrings).
        size_clause = f"{char_clause} — index entries are too long"

    if cutoff_real == 0:
        # Not even line 1 fit -- there is no complete dropped line to quote,
        # so the continuation names how many characters of line 1 itself
        # were kept before the cut, not a line range.
        continuation = (
            f"everything after the first {NATIVE_INDEX_MAX_CHARS} characters "
            f"of line 1 was cut off"
        )
    else:
        snippet = (
            _native_index_warning_snippet(written_lines[cutoff])
            if cutoff < len(written_lines)
            else ""
        )
        continuation = (
            f'{cut_count} of {total_lines} lines were cut off, starting at line '
            f'{start_line} ("{snippet}")'
        )

    warning = (
        f"\n> WARNING: MEMORY.md is {size_clause}. Only part of it was loaded: "
        f"{continuation}. Keep index entries to one line under "
        f"~200 chars; move detail into topic files.\n"
    )
    return truncated + warning, True


def _native_grep_tool_schema() -> dict[str, Any]:
    return {
        "name": GREP_TOOL_NAME,
        "description": (
            "Search the markdown files in the memory directory for a substring or regex "
            "pattern (case-insensitive). Returns matching lines with their file path and "
            "line number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "substring or regex"}},
            "required": ["pattern"],
        },
    }


def _native_read_tool_schema() -> dict[str, Any]:
    return {
        "name": READ_TOOL_NAME,
        "description": "Read the full contents of one file in the memory directory, by path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "file path to read"}},
            "required": ["path"],
        },
    }


def _resolve_under_memory_dir(memory_dir: Path, raw_path: str) -> Path | None:
    """Resolve *raw_path* against *memory_dir*, refusing anything that
    escapes it (an absolute path elsewhere, or a ``..`` traversal). Returns
    ``None`` rather than raising -- a confined tool executor reports an
    error string back to the model, exactly like a real file-not-found,
    never crashes the rollout."""
    base = memory_dir.resolve()
    candidate = Path(raw_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return resolved


def _native_grep_executor(memory_dir: Path, tool_input: Mapping[str, Any]) -> str:
    pattern_raw = str(tool_input.get("pattern", ""))
    if not pattern_raw:
        return "error: empty pattern"
    try:
        pattern = re.compile(pattern_raw, re.IGNORECASE)
    except re.error as exc:
        return f"error: invalid pattern: {exc}"
    matches: list[str] = []
    total_bytes = 0
    for path in sorted(memory_dir.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            clipped_line = line.strip()[:_NATIVE_GREP_MAX_LINE_CHARS]
            entry = f"{path.relative_to(memory_dir)}:{lineno}: {clipped_line}"
            entry_bytes = len(entry.encode("utf-8")) + 1  # +1 for the joining newline
            if (
                len(matches) >= _NATIVE_GREP_MAX_MATCHES
                or total_bytes + entry_bytes > _NATIVE_GREP_MAX_TOTAL_BYTES
            ):
                matches.append("[truncated: max matches or max bytes reached]")
                return "\n".join(matches)
            matches.append(entry)
            total_bytes += entry_bytes
    return "\n".join(matches) if matches else "no matches"


def _native_read_executor(
    memory_dir: Path, tool_input: Mapping[str, Any], *, loaded: dict[str, str]
) -> str:
    # Resolve ONCE and reuse for both the confinement check AND the later
    # relative-path key -- Quine review, issue athenaeum#1733: a two-arg
    # comparison of a RESOLVED path (what ``_resolve_under_memory_dir``
    # returns) against an UNRESOLVED ``memory_dir`` raises ``ValueError`` on
    # any host where the base scratch directory is itself a symlink (macOS's
    # ``/tmp`` -> ``/private/tmp`` is exactly this case), aborting every
    # ``read`` call and, per :func:`run_api_tool_loop`'s never-raise
    # contract, the whole probe. Using the SAME resolved base throughout
    # this function is what makes the comparison symlink-safe.
    base = memory_dir.resolve()
    raw_path = str(tool_input.get("path", ""))
    resolved = _resolve_under_memory_dir(memory_dir, raw_path)
    if resolved is None or not resolved.is_file():
        return f"error: path not found or outside the memory directory: {raw_path!r}"
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return f"error: could not read {raw_path!r}: {exc}"
    truncated = text[:_NATIVE_READ_MAX_BYTES]
    loaded[str(resolved.relative_to(base))] = truncated
    return truncated


def truncate_native_index(
    text: str,
    *,
    max_lines: int = NATIVE_INDEX_MAX_LINES,
    max_chars: int = NATIVE_INDEX_MAX_CHARS,
) -> str:
    """Apply Claude Code's documented auto-memory load cap to *text* --
    the first *max_lines* lines, THEN (within that window) cut further by
    cumulative STRING LENGTH once past *max_chars* (design doc §2/§4) -- so
    api-mode NATIVE_INDEX can inject the SAME truncated index CLI mode
    receives from Claude Code's own loader, rather than the full,
    untruncated ``MEMORY.md``.

    Counted by Python ``str`` length, NOT UTF-8 encoded bytes (Quine review,
    issue athenaeum#1733) -- the real loader measures string length the way
    its own runtime does, and this corpus's index lines contain multi-byte
    characters (an em dash between name and description) that a byte count
    would over-weight relative to the real cap. The line cap is applied
    FIRST and the char cap only within what remains, matching the real
    loader's own two-stage behavior, not two independent caps taken as a
    minimum over the whole file.

    Counts whole lines only: a line that would push the cumulative char
    count past *max_chars* is dropped in full, never split -- matching the
    CLI path's own observation (`_native_index_coverage`'s docstring) that
    the real loader's truncation is a content boundary, not an arbitrary
    character cut. Never raises; an empty *text* returns ``""``.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    total_chars = 0
    for line in lines[:max_lines]:
        line_chars = len(line)
        if total_chars + line_chars > max_chars:
            break
        out.append(line)
        total_chars += line_chars
    return "".join(out)


def _api_response_blocks(response: Any) -> list[dict[str, Any]]:
    """Normalize ``response.content`` into plain dicts -- ``{"type": "text",
    "text": ...}`` or ``{"type": "tool_use", "id": ..., "name": ...,
    "input": ...}`` -- so the loop below (and the transcript it records)
    never touches SDK block objects directly."""
    blocks: list[dict[str, Any]] = []
    for block in getattr(response, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            blocks.append({"type": "text", "text": str(getattr(block, "text", "") or "")})
        elif btype == "tool_use":
            raw_input = getattr(block, "input", None)
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(getattr(block, "id", "") or ""),
                    "name": str(getattr(block, "name", "") or ""),
                    "input": raw_input if isinstance(raw_input, dict) else {},
                }
            )
    return blocks


def run_api_tool_loop(
    *,
    user_prompt: str,
    system: str,
    tools: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], str],
    client: Any,
    session: EvalSession,
    model: str,
    max_turns: int = _API_LOOP_MAX_TURNS,
) -> tuple[str, list[ToolCall], list[TurnTokenUsage], int, list[dict[str, Any]]]:
    """Drive one Anthropic Messages API tool-use loop to completion (or
    *max_turns*), starting from a single user message.

    *system* has NO default (Quine review, issue athenaeum#1733): every
    caller must pass an arm-appropriate prompt explicitly -- there is no
    safe generic fallback, and the single-shot arms' own ``_SYSTEM_PROMPT``
    ("answer using ONLY the context supplied") is actively WRONG for a
    tool-using loop, since it discourages the very tool call this loop
    exists to observe. A missing *system* is a caller bug, not a case to
    silently paper over with a default that would be wrong for every arm
    that forgot to pass one.

    Returns ``(answer, tool_calls, turn_tokens, turn_count, transcript)`` --
    the exact tuple every api-mode arm function assembles its
    :class:`RolloutRecord` from. *tool_executor* is called as
    ``tool_executor(name, tool_input)`` for every ``tool_use`` block the
    model emits, in order, and must never raise (a confined executor
    returns an error STRING on a bad input, per :func:`_native_read_executor`
    / :func:`_native_grep_executor`'s own contract) -- an executor that
    raises would abort the rollout the same way a genuine ``claude -p``
    spawn failure does, which is not this loop's contract: choosing a bad
    tool input is the model's mistake to observe, not the harness's to
    crash on.

    Never raises on a loop that never calls a tool at all (a legitimate
    recorded outcome, same as :func:`parse_pull_stream`), nor on one that
    exhausts *max_turns* without a final text answer (returns whatever text
    was last seen, possibly "").
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    transcript: list[dict[str, Any]] = [
        {"type": "user", "message": {"content": [{"type": "text", "text": user_prompt}]}}
    ]
    tool_calls: list[ToolCall] = []
    turn_tokens: list[TurnTokenUsage] = []
    answer = ""
    turn = 0

    for _ in range(max_turns):
        turn += 1
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=messages,
            tools=tools,
        )
        usage = _observe_turn(session, model, response, turn=turn)
        turn_tokens.append(usage)
        blocks = _api_response_blocks(response)
        transcript.append(
            {
                "type": "assistant",
                "message": {
                    "content": blocks,
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    },
                },
            }
        )
        messages.append({"role": "assistant", "content": blocks})

        text_blocks = [b["text"] for b in blocks if b["type"] == "text" and b["text"]]
        if text_blocks:
            answer = text_blocks[-1]
        tool_use_blocks = [b for b in blocks if b["type"] == "tool_use"]
        stop_reason = getattr(response, "stop_reason", None)
        if not tool_use_blocks or (stop_reason is not None and stop_reason != "tool_use"):
            break

        tool_result_content: list[dict[str, Any]] = []
        for block in tool_use_blocks:
            name = block["name"]
            tool_input = block["input"]
            query = str(tool_input.get("query", tool_input))
            tool_calls.append(ToolCall(name=name, query=query))
            try:
                result_text = tool_executor(name, tool_input)
            except Exception as exc:  # noqa: BLE001 -- never-raise contract (Quine review)
                # A confined executor is documented to return an error
                # STRING rather than raise -- but "never raise" is this
                # loop's OWN contract, not something it may assume every
                # executor upholds perfectly. Catching here turns an
                # unexpected executor exception into an ordinary
                # tool_result the model sees (exactly like a bad path or a
                # bad regex already produces), rather than aborting the
                # whole rollout the way a genuine claude -p spawn failure
                # does -- that asymmetry would be a mode-dependent error
                # surface, which this module's parity goal forbids.
                result_text = f"error: tool {name!r} raised {exc.__class__.__name__}: {exc}"
            tool_result_content.append(
                {"type": "tool_result", "tool_use_id": block["id"], "content": result_text}
            )
        transcript.append({"type": "user", "message": {"content": tool_result_content}})
        messages.append({"role": "user", "content": tool_result_content})

    return answer, tool_calls, turn_tokens, turn, transcript


def run_pull_api(
    probe: Probe,
    wiki_root: Path,
    cache_dir: Path,
    corpus_scale: str,
    *,
    client: Any,
    session: EvalSession,
    model: str = DEFAULT_ROLLOUT_MODEL,
    search_backend: str = "fts5",
) -> RolloutRecord:
    """API-mode PULL arm: the ``recall`` and ``read_entity`` tools are served
    in-process (direct calls to :func:`athenaeum.mcp_server.recall_search` and
    :func:`athenaeum.mcp_server.entity_read` over *wiki_root* -- exactly the
    functions the shipped MCP server itself calls, same as
    :func:`run_push_pages_upper_bound`'s reuse), rather than spawned via a
    scoped ``claude -p --mcp-config``. Those two, and nothing else, are what
    the real server serves a PULL arm (issue athenaeum#1756). Takes
    *wiki_root* directly (unlike
    :func:`run_pull`, which takes the knowledge root and lets ``athenaeum
    serve`` derive the wiki root itself) because there is no subprocess
    here to do that derivation.
    """
    # Issue athenaeum#1761: ``wiki_root.parent`` is the knowledge root by
    # ``tests.evals.corpus.Corpus.materialize``'s own layout (``wiki =
    # root / "wiki"``) -- the SAME invariant ``_serve_read_entity`` already
    # relies on for this exact parameter. Loaded ONCE here, not per tool
    # call: an operator opting into a relevance floor writes
    # ``athenaeum.yaml`` into that knowledge root before this arm ever runs
    # (never during it), so the file is stable for the whole rollout.
    config = load_config(wiki_root.parent)

    def _executor(name: str, tool_input: dict[str, Any]) -> str:
        if name == READ_ENTITY_TOOL_NAME:
            return _serve_read_entity(wiki_root, tool_input)
        if name != RECALL_TOOL_NAME:
            return f"error: unknown tool {name!r}"
        # *search_backend* is the SAME parameter ``run_probe_all_arms``
        # threads to every other arm (CLI PULL's own ``--search-backend``
        # included) -- never a second, independently-defaulted value that
        # could silently diverge between modes (Quine review, item 9).
        # ``extra_roots``/``caller_audience``/``tool_use_id``/
        # ``session_resolver`` are the real server's OTHER ``recall_search``
        # kwargs (see ``athenaeum.mcp_server.create_server``'s own ``recall``
        # closure) and are intentionally absent here: this materialized eval
        # corpus has no scope-aware audience to pass, and there is no MCP
        # tool-use session for the resolver to key on -- api mode measures
        # the SAME retrieval call with those inputs at their defaults, not a
        # degraded one. ``config`` IS threaded (issue athenaeum#1761): an
        # operator-written relevance floor must actually apply to this tool
        # call for it to mean anything, exactly like the real server's
        # ``recall`` closure threading its own resolved config.
        return recall_search(
            wiki_root,
            str(tool_input.get("query", "")),
            top_k=int(tool_input.get("top_k") or 5),
            search_backend=search_backend,
            cache_dir=cache_dir,
            with_pii=bool(tool_input.get("with_pii", False)),
            history=bool(tool_input.get("history", False)),
            type_filter=tool_input.get("type"),
            config=config,
        )

    answer, tool_calls, turn_tokens, turn_count, transcript = run_api_tool_loop(
        user_prompt=probe.query,
        tools=[_recall_tool_schema(wiki_root), _read_entity_tool_schema()],
        tool_executor=_executor,
        client=client,
        session=session,
        model=model,
        system=_PULL_API_SYSTEM_PROMPT,
    )
    return RolloutRecord(
        arm=Arm.PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        turn_tokens=turn_tokens,
        tool_calls=tool_calls,
        recall_called=bool(tool_calls),
        injected_context_tokens=None,
        turn_count=turn_count,
        transcript=transcript,
        mode="api",
    )


def run_push_breadcrumb_pull_api(
    probe: Probe,
    knowledge_root: Path,
    hook_home: Path,
    cache_dir: Path,
    corpus_scale: str,
    *,
    client: Any,
    session: EvalSession,
    model: str = DEFAULT_ROLLOUT_MODEL,
    search_backend: str = "fts5",
    context_fn: Callable[..., str] | None = None,
    wiki_root: Path | None = None,
) -> RolloutRecord:
    """API-mode PUSH_BREADCRUMB_PULL: the breadcrumb is assembled by
    ACTUALLY RUNNING the shipped hooks, exactly like
    :func:`run_push_breadcrumb_pull` -- only the tool-use loop itself is
    API-backed rather than a ``claude -p`` spawn. *wiki_root* defaults to
    ``knowledge_root / "wiki"`` (:meth:`tests.evals.corpus.Corpus.materialize`'s
    own layout), matching what ``athenaeum serve --path knowledge_root``
    would derive for the CLI path.
    """
    assemble = context_fn or build_push_breadcrumb_context
    breadcrumb = assemble(knowledge_root, hook_home, probe.query)
    resolved_wiki_root = wiki_root if wiki_root is not None else knowledge_root / "wiki"
    prompt_text = f"{breadcrumb}\n\n{probe.query}" if breadcrumb else probe.query
    # Issue athenaeum#1761: *knowledge_root* is already the exact root
    # ``assemble`` (the shipped hook) just read ``athenaeum.yaml`` from via
    # ``KNOWLEDGE_ROOT`` -- loading it again here, once, keeps the ``recall``
    # tool call below applying the SAME operator-configured floor.
    config = load_config(knowledge_root)

    def _executor(name: str, tool_input: dict[str, Any]) -> str:
        if name == READ_ENTITY_TOOL_NAME:
            return _serve_read_entity(resolved_wiki_root, tool_input)
        if name != RECALL_TOOL_NAME:
            return f"error: unknown tool {name!r}"
        # Same *search_backend* parity note as :func:`run_pull_api`'s own
        # executor -- see that docstring comment for which real server
        # kwargs are intentionally absent here. ``config`` IS threaded, same
        # reasoning as that function.
        return recall_search(
            resolved_wiki_root,
            str(tool_input.get("query", "")),
            top_k=int(tool_input.get("top_k") or 5),
            search_backend=search_backend,
            cache_dir=cache_dir,
            with_pii=bool(tool_input.get("with_pii", False)),
            history=bool(tool_input.get("history", False)),
            type_filter=tool_input.get("type"),
            config=config,
        )

    answer, tool_calls, turn_tokens, turn_count, loop_transcript = run_api_tool_loop(
        user_prompt=prompt_text,
        tools=[_recall_tool_schema(resolved_wiki_root), _read_entity_tool_schema()],
        tool_executor=_executor,
        client=client,
        session=session,
        model=model,
        system=_PULL_API_SYSTEM_PROMPT,
    )
    # Same transcript[0] shape run_push_breadcrumb_pull uses, so
    # north_star_report._push_delivered_text's transcript[0]["pushed_context"]
    # read needs no mode branch either.
    transcript = [{"pushed_context": breadcrumb}, *loop_transcript]
    return RolloutRecord(
        arm=Arm.PUSH_BREADCRUMB_PULL,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        turn_tokens=turn_tokens,
        tool_calls=tool_calls,
        recall_called=bool(tool_calls),
        injected_context_tokens=estimate_tokens(breadcrumb) if breadcrumb else 0,
        turn_count=turn_count,
        transcript=transcript,
        mode="api",
    )


def run_native_index_api(
    probe: Probe,
    materialize_root: Path,
    corpus_scale: str,
    *,
    client: Any,
    session: EvalSession,
    model: str = DEFAULT_ROLLOUT_MODEL,
) -> RolloutRecord:
    """API-mode NATIVE_INDEX: the harness itself builds the full index,
    applies the documented truncation (:func:`truncate_native_index`), and
    injects the truncated result as the FIRST user turn (design doc §4) --
    prepended to the probe's query in one message, the same shape
    :func:`run_push_breadcrumb_pull` already uses for its own caller-
    assembled context. ``grep``/``read`` tools are served over the
    materialized topic-file directory so the model can still open a page
    the index names.

    Index coverage is computed directly from what THIS function injected
    (the truncated text), never re-derived by reading anything back --
    unlike the CLI path (:func:`read_loaded_memory_files`), there is no
    separate loader here whose behavior needs observing after the fact:
    the harness IS the loader in api mode.
    """
    corpus = build_corpus(corpus_scale)
    memory_dir = materialize_native_memory(corpus, materialize_root, write_index=True)
    index_path = memory_dir / "MEMORY.md"
    written_index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    truncated_index_text = truncate_native_index(written_index_text)
    # Inject the SAME ``WARNING:`` marker text Claude Code's own truncated
    # load ends with (Quine review, issue athenaeum#1733) -- so the model
    # sees an identical truncation signal either mode produces, and
    # ``injected_index_text`` (not the bare capped text) is what actually
    # goes in front of the model AND what coverage is computed from below.
    injected_index_text, was_truncated = _native_index_text_with_warning(
        written_index_text, truncated_index_text
    )
    coverage = _native_index_coverage(corpus, written_index_text, injected_index_text)
    coverage_dict = dataclasses.asdict(coverage)
    # Rename/override for api mode (Quine review): ``truncated_by_claude_code``
    # is a claim about WHO truncated the index, and in api mode that is
    # always false -- THIS harness truncated it, not a Claude Code process.
    # ``truncated_by_harness`` is the api-mode-only fact
    # ``_native_index_text_with_warning`` already decided; recording it under
    # its own name (rather than overloading the CLI field) means a reader of
    # a stored row can tell which of the two ever fired, per row, without
    # cross-referencing ``mode``.
    coverage_dict["truncated_by_claude_code"] = False
    coverage_dict["truncated_by_harness"] = was_truncated

    prompt_text = f"Memory index (MEMORY.md):\n\n{injected_index_text}\n\nQuestion: {probe.query}"
    loaded_files: dict[str, str] = {}

    def _executor(name: str, tool_input: dict[str, Any]) -> str:
        if name == GREP_TOOL_NAME:
            return _native_grep_executor(memory_dir, tool_input)
        if name == READ_TOOL_NAME:
            return _native_read_executor(memory_dir, tool_input, loaded=loaded_files)
        return f"error: unknown tool {name!r}"

    answer, tool_calls, turn_tokens, turn_count, loop_transcript = run_api_tool_loop(
        user_prompt=prompt_text,
        tools=[_native_grep_tool_schema(), _native_read_tool_schema()],
        tool_executor=_executor,
        client=client,
        session=session,
        model=model,
        system=_native_index_system_prompt(memory_dir),
    )
    transcript = [
        {
            "native_memory": {
                **coverage_dict,
                "loaded_index_text": injected_index_text,
                # Issue athenaeum#1831: topic files the model actually
                # opened via the `read` tool during this turn -- the same
                # per-page delivery evidence `run_native_grep_api` already
                # records, and cli-mode `run_native_index` now records too.
                "loaded_memory_files": loaded_files,
            }
        },
        *loop_transcript,
    ]
    return RolloutRecord(
        arm=Arm.NATIVE_INDEX,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        turn_tokens=turn_tokens,
        tool_calls=tool_calls,
        recall_called=False,
        injected_context_tokens=None,
        turn_count=turn_count,
        transcript=transcript,
        mode="api",
    )


def run_native_grep_api(
    probe: Probe,
    materialize_root: Path,
    corpus_scale: str,
    *,
    client: Any,
    session: EvalSession,
    model: str = DEFAULT_ROLLOUT_MODEL,
) -> RolloutRecord:
    """API-mode NATIVE_GREP: same materialized topic files as
    :func:`run_native_grep`, no ``MEMORY.md``, harness-served ``grep``/
    ``read`` tools over the directory.
    """
    corpus = build_corpus(corpus_scale)
    memory_dir = materialize_native_memory(corpus, materialize_root, write_index=False)
    prompt_text = (
        f"The knowledge base is a directory of markdown files at {memory_dir}. "
        f"Use your grep and read tools to find the answer.\n\n"
        f"Question: {probe.query}"
    )
    loaded_files: dict[str, str] = {}

    def _executor(name: str, tool_input: dict[str, Any]) -> str:
        if name == GREP_TOOL_NAME:
            return _native_grep_executor(memory_dir, tool_input)
        if name == READ_TOOL_NAME:
            return _native_read_executor(memory_dir, tool_input, loaded=loaded_files)
        return f"error: unknown tool {name!r}"

    answer, tool_calls, turn_tokens, turn_count, loop_transcript = run_api_tool_loop(
        user_prompt=prompt_text,
        tools=[_native_grep_tool_schema(), _native_read_tool_schema()],
        tool_executor=_executor,
        client=client,
        session=session,
        model=model,
        system=_native_grep_system_prompt(memory_dir),
    )
    transcript = [
        {"native_memory": {"memory_dir": str(memory_dir), "loaded_memory_files": loaded_files}},
        *loop_transcript,
    ]
    return RolloutRecord(
        arm=Arm.NATIVE_GREP,
        probe_id=probe.id,
        probe_class=probe.probe_class,
        corpus_scale=corpus_scale,
        answer=answer,
        turn_tokens=turn_tokens,
        tool_calls=tool_calls,
        recall_called=False,
        injected_context_tokens=None,
        turn_count=turn_count,
        transcript=transcript,
        mode="api",
    )


# ---------------------------------------------------------------------------
# Native WRITER — Phase 2 write-path arm (issue athenaeum#1726, design lock
# docs/design/native-memory-baseline.md §5)
#
# Every arm above hands the model a FINISHED store and grades how it reads
# it. This section is the write half: a sequence of `claude -p` sessions
# with auto memory enabled, each fed one slice of a
# `tests.evals.corpus.Observation` stream on stdin, writing whatever it
# chooses to save into a FRESH `autoMemoryDirectory`. It reuses the
# athenaeum#1725 native-arm machinery verbatim (`seed_native_claude_config`,
# `build_native_settings`, `build_native_argv`, `parse_stream`,
# `read_loaded_memory_files`) rather than a second implementation of any of
# it -- this is the write-side counterpart to `_spawn_native`, not a
# replacement for it.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class NativeWriterSession:
    """One `claude -p` session in the writer sequence -- the SAME
    transcript-capture shape the read arms use (`tool_calls`/`turn_tokens`/
    `transcript`, via :func:`parse_stream`), so what the model chose to save
    (or not save) in THIS session is auditable exactly like a NATIVE_INDEX
    or NATIVE_GREP rollout's is. ``observation_uid`` is the ground-truth
    input this session was fed, so a session record can be joined back to
    the observation stream's own ``page_uid``/``answer_tokens``."""

    session_index: int
    observation_uid: str
    tool_calls: list[ToolCall]
    turn_tokens: list[TurnTokenUsage]
    turn_count: int
    transcript: list[dict[str, Any]]
    #: Issue athenaeum#1774 Quine review (Should 2): ``True`` when this
    #: session's :func:`run_api_tool_loop` call ran out of
    #: ``_WRITER_API_LOOP_MAX_TURNS`` turns while the model still had a
    #: pending tool call -- i.e. the loop was cut off, not merely reaching a
    #: natural end on its last permitted turn. Always ``False`` for a
    #: ``claude -p`` (CLI-mode) session: that path has no comparable
    #: harness-imposed turn cap to exhaust.
    turns_exhausted: bool = False


@dataclasses.dataclass(frozen=True)
class NativeWriterResult:
    """What one full writer sequence produced: every session's transcript,
    plus what ended up in the memory directory AFTERWARDS -- read directly
    off disk (the model's own write), never re-derived from a transcript
    parse. ``memory_files`` maps each file's path (relative to
    ``memory_dir``) to its full text."""

    sessions: list[NativeWriterSession]
    memory_dir: Path
    memory_files: dict[str, str]
    #: Issue athenaeum#1774: which execution path produced this result --
    #: ``"cli"`` (``claude -p``, :func:`run_native_writer`) or ``"api"``
    #: (:func:`run_native_writer_api`), the same two values and the same
    #: back-compat-default reasoning as :attr:`RolloutRecord.mode`. Every
    #: constructor in this module sets it explicitly.
    mode: str = "cli"
    #: Issue athenaeum#1774 Quine review (Must 1): ``"reconstructed"`` for
    #: an api-mode result -- :func:`_native_writer_system_prompt` mirrors
    #: Claude Code's DOCUMENTED auto-memory writing behaviour, not its own
    #: closed-source write-side prompt (which is not extractable), and
    #: diverges from it in ways with OPPOSITE effect on measured native
    #: write cost (see that function's own docstring for the two directions
    #: named). ``None`` for a ``"cli"`` result: :func:`run_native_writer`
    #: observes the real Claude Code prompt directly, so there is nothing to
    #: label as reconstructed. A report reading this field labels every
    #: api-mode Phase 2 number an approximation pending the CLI spot-check,
    #: never silently pools it with a cli-mode row as equally faithful.
    prompt_fidelity: str | None = None

    @property
    def total_tool_calls(self) -> int:
        return sum(len(s.tool_calls) for s in self.sessions)


#: The instruction every writer session is prompted with, ahead of the raw
#: observation text. Deliberately spare -- this arm measures what the
#: model's OWN auto-memory judgment chooses to keep from an ordinary note,
#: not what it does when explicitly coached on HOW to file it (that would
#: confound the write-path comparison with a prompt-engineering effect
#: neither system's real usage gets).
_WRITER_PROMPT_PREFIX = "Here is a note from today. Save anything worth remembering.\n\n"


def run_native_writer(
    observations: Sequence[Observation],
    materialize_root: Path,
    *,
    claude_binary: str = "claude",
    model: str = DEFAULT_ROLLOUT_MODEL,
    timeout: float = 120.0,
) -> NativeWriterResult:
    """Drive one `claude -p` session per entry in *observations*, in order,
    all sharing the SAME fresh auto-memory directory under
    *materialize_root* -- so a later session can see (and choose to update)
    what an earlier one wrote, exactly like a real multi-day memory store.

    Isolated with `CLAUDE_CONFIG_DIR` the same way `_spawn_native` isolates
    the read arms (`seed_native_claude_config`), and the same empty scoped
    MCP config (`--strict-mcp-config` over ``{"mcpServers": {}}``) so the
    athenaeum `recall` tool is absent here too -- this measures the model's
    OWN write judgment, not anything athenaeum contributes. Each
    observation's body goes on STDIN, never argv (issue athenaeum#543 L4,
    the same discipline every other arm in this module follows).

    Raises only on a genuine spawn failure (binary missing, timeout) --
    mirrors :func:`run_pull`'s contract exactly (issue athenaeum#1726 AC3):
    a session that chooses to write NOTHING is a recorded outcome (an empty
    ``tool_calls`` list on that session's record), never an error.
    """
    if shutil.which(claude_binary) is None:
        raise RuntimeError(f"{claude_binary!r} not found on PATH")

    memory_dir = materialize_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    config_dir = materialize_root / "claude-config"
    seed_native_claude_config(config_dir)

    settings_path = materialize_root / "native-writer-settings.json"
    settings_path.write_text(json.dumps(build_native_settings(memory_dir)), encoding="utf-8")
    mcp_config_path = materialize_root / "native-writer-mcp-config.json"
    mcp_config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    # Issue athenaeum#1753: the reference-tag instruction is deliberately
    # withheld here. It is an ANSWER-shaping contract for the graded read
    # path; these are WRITE sessions whose output is memory files, not a
    # graded answer, and telling a writer to append `[ref: ...]` would only
    # risk polluting what it saves. Every arm that produces a graded answer
    # carries the instruction; this one produces none.
    argv = build_native_argv(
        claude_binary,
        settings_path,
        mcp_config_path,
        memory_dir,
        model,
        append_system_prompt=None,
    )
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}

    sessions: list[NativeWriterSession] = []
    for i, observation in enumerate(observations):
        proc = subprocess.run(
            argv,
            input=f"{_WRITER_PROMPT_PREFIX}{observation.body}",
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
        parsed = parse_stream((proc.stdout or "").splitlines(), tool_names=None)
        sessions.append(
            NativeWriterSession(
                session_index=i,
                observation_uid=observation.uid,
                tool_calls=parsed.tool_calls,
                turn_tokens=parsed.turn_tokens,
                turn_count=parsed.turn_count,
                transcript=parsed.transcript,
            )
        )

    memory_files = {
        str(path.relative_to(memory_dir)): path.read_text(encoding="utf-8")
        for path in sorted(memory_dir.rglob("*"))
        if path.is_file()
    }
    return NativeWriterResult(
        sessions=sessions,
        memory_dir=memory_dir,
        memory_files=memory_files,
        mode="cli",
        prompt_fidelity=None,
    )


# ---------------------------------------------------------------------------
# Native WRITER, api mode (issue athenaeum#1774) — the Phase 2 blocking
# prerequisite. run_native_writer above spawns `claude -p`, which requires a
# logged-in CLI; the grid's default is `--mode api`
# (docs/design/native-memory-baseline.md §4), so Phase 2 cannot run under
# `workflow_dispatch` without this arm. Drives run_api_tool_loop the same
# way run_native_index_api/run_native_grep_api do, one session per
# Observation, all sharing the SAME memory directory across the stream --
# the multi-day property run_native_writer's own docstring establishes.
#
# Tool surface: the read pair every native read arm already serves
# (`read`/`grep`, `_native_read_executor`/`_native_grep_executor`) PLUS a
# write pair this arm adds (`write`/`edit`) and a `list` tool -- the same
# four-verb surface Claude Code's own file tools give its auto-memory
# writer (create/overwrite a file, patch one in place, enumerate what
# exists, read one back). Every one of the five is confined by
# `_resolve_under_memory_dir`, exactly like the read arms: a writer must not
# be able to escape the memory directory any more than a reader may.
# ---------------------------------------------------------------------------

WRITE_TOOL_NAME = "write"
EDIT_TOOL_NAME = "edit"
LIST_TOOL_NAME = "list"


def _native_write_tool_schema() -> dict[str, Any]:
    return {
        "name": WRITE_TOOL_NAME,
        "description": (
            "Create or overwrite one file in the memory directory with the given "
            "content. Creates parent directories as needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "file path to write, relative to the memory directory",
                },
                "content": {"type": "string", "description": "full text to write"},
            },
            "required": ["path", "content"],
        },
    }


def _native_edit_tool_schema() -> dict[str, Any]:
    return {
        "name": EDIT_TOOL_NAME,
        "description": (
            "Edit one existing file in the memory directory by replacing an exact, "
            "unique occurrence of old_string with new_string."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "file path to edit, relative to the memory directory",
                },
                "old_string": {
                    "type": "string",
                    "description": "exact text to replace; must occur exactly once in the file",
                },
                "new_string": {"type": "string", "description": "replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    }


def _native_list_tool_schema() -> dict[str, Any]:
    return {
        "name": LIST_TOOL_NAME,
        "description": "List every file currently saved in the memory directory, by path.",
        "input_schema": {"type": "object", "properties": {}},
    }


def _native_write_executor(memory_dir: Path, tool_input: Mapping[str, Any]) -> str:
    raw_path = str(tool_input.get("path", ""))
    if not raw_path:
        return "error: empty path"
    content = str(tool_input.get("content", ""))
    resolved = _resolve_under_memory_dir(memory_dir, raw_path)
    if resolved is None:
        return f"error: path outside the memory directory: {raw_path!r}"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"error: could not write {raw_path!r}: {exc}"
    return f"wrote {len(content)} characters to {resolved.relative_to(memory_dir.resolve())}"


def _native_edit_executor(memory_dir: Path, tool_input: Mapping[str, Any]) -> str:
    raw_path = str(tool_input.get("path", ""))
    if not raw_path:
        return "error: empty path"
    old_string = str(tool_input.get("old_string", ""))
    if not old_string:
        return "error: empty old_string"
    new_string = str(tool_input.get("new_string", ""))
    resolved = _resolve_under_memory_dir(memory_dir, raw_path)
    if resolved is None or not resolved.is_file():
        return f"error: path not found or outside the memory directory: {raw_path!r}"
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return f"error: could not read {raw_path!r}: {exc}"
    occurrences = text.count(old_string)
    if occurrences == 0:
        return f"error: old_string not found in {raw_path!r}"
    if occurrences > 1:
        return f"error: old_string is not unique in {raw_path!r} ({occurrences} occurrences)"
    try:
        resolved.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
    except OSError as exc:
        return f"error: could not write {raw_path!r}: {exc}"
    return f"edited {resolved.relative_to(memory_dir.resolve())}"


def _native_list_executor(memory_dir: Path) -> str:
    base = memory_dir.resolve()
    paths = sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())
    return "\n".join(paths) if paths else "(empty)"


#: Issue athenaeum#1774: mirrors Claude Code's DOCUMENTED auto-memory
#: WRITING behaviour (design doc §2: "The model decides what to write, when,
#: and how to keep the index short") the same way
#: `_native_index_system_prompt`/`_native_grep_system_prompt` mirror its
#: reading behaviour -- NOT a literal quote of Claude Code's own internal
#: system-prompt text.
#:
#: That distinction is load-bearing here in a way it is not for those two:
#: the truncation notice template those two arms reproduce was EXTRACTED
#: from the 2.1.274 binary (design doc §4) and is byte-verified. No
#: comparable extraction exists for the writing-side prompt -- CLI-mode
#: `run_native_writer` sends NO explicit write-judgment instructions at all
#: (`build_native_argv(..., append_system_prompt=None)`, that function's own
#: docstring: "the WRITE path is deliberately outside the contract"), relying
#: entirely on Claude Code's OWN closed-source auto-memory system prompt.
#: This text is this harness's best-effort reproduction of the DOCUMENTED
#: contract (<https://code.claude.com/docs/en/memory>, design doc §2), not a
#: byte-exact copy, and that gap is exactly what the CLI-mode spot-check
#: (`run_native_writer`) exists to catch (design doc §4: "the existing
#: claude -p path stays as an optional fidelity spot-check").
#:
#: Two KNOWN divergences from the real prompt, with OPPOSITE effect on
#: measured native write cost (issue athenaeum#1774 Quine review, Must 1),
#: so neither can be assumed to net out:
#:
#: 1. The harness's own `- <name> — <description>` index-line format is
#:    handed to the model as an instruction here, rather than emerging from
#:    the model's own unprompted filing judgment the way it would in a real
#:    session -- this biases toward BETTER, cheaper filing than a real
#:    auto-memory writer produces (understating native write cost / write
#:    quality relative to reality).
#: 2. The explicit `list`-before-writing and check-before-`edit`
#:    instructions add tool calls a real auto-memory writer is not
#:    documented to be told to make -- this biases toward MORE turns and
#:    higher token spend than a real writer incurs (overstating native
#:    write cost relative to reality).
#:
#: Because these two push in opposite directions, a Phase 2 number produced
#: from this prompt is an APPROXIMATION, not a calibrated substitute for the
#: real prompt -- :attr:`NativeWriterResult.prompt_fidelity` labels every
#: api-mode row `"reconstructed"` so a report can say so, and the CLI-mode
#: spot-check (`run_native_writer`) remains the only path that observes
#: Claude Code's actual write-side prompt.
_NATIVE_WRITER_SYSTEM_PROMPT_TEMPLATE = (
    "You are Claude Code working on a project whose memory directory is at "
    "{memory_dir}. You maintain your own long-term memory there: markdown "
    "topic files, plus an index file named MEMORY.md with one line per topic "
    "in the form `- <name> — <description>`. When you learn something worth "
    "remembering, decide for yourself whether it belongs in an existing topic "
    "file (use `edit` to update it) or a new one (use `write` to create it), "
    "and keep MEMORY.md's index line for that topic current and short -- "
    f"only the first {NATIVE_INDEX_MAX_LINES} lines or {NATIVE_INDEX_MAX_CHARS} "
    "characters of MEMORY.md, whichever comes first, are loaded at the start "
    "of every session; nothing past that is loaded, so keep the most "
    "important entries near the top and each one to about one line. Use "
    "`list` to see what is already saved and `read`/`grep` to check a topic "
    "file's current content before editing it. Use your own judgment about "
    "what is worth remembering -- not every note needs to be saved. If "
    "nothing here is worth saving, do not write anything."
)


def _native_writer_system_prompt(memory_dir: Path) -> str:
    return _NATIVE_WRITER_SYSTEM_PROMPT_TEMPLATE.format(memory_dir=memory_dir)


def _api_loop_turns_exhausted(
    turn_count: int, max_turns: int, transcript: list[dict[str, Any]]
) -> bool:
    """Whether a :func:`run_api_tool_loop` call was cut off by *max_turns*
    while the model still had a pending tool call, rather than reaching a
    natural end (no tool use, or a non-``tool_use`` stop reason) on exactly
    its last permitted turn (issue athenaeum#1774 Quine review, Should 2).

    Computed from the returned tuple alone -- :func:`run_api_tool_loop`'s
    own signature is unchanged, so every OTHER caller in this module is
    unaffected. The distinguishing fact is transcript shape: the loop
    appends a tool-result ``"user"`` transcript entry only when it is about
    to CONTINUE (i.e. it did not break), so that entry is the LAST thing in
    the transcript if and only if the loop ran out of turns mid-tool-use.
    A natural end always breaks BEFORE appending that entry, so the
    transcript ends on the ``"assistant"`` entry instead. ``turn_count ==
    max_turns`` alone is not sufficient: a natural end can also happen to
    land on the final permitted turn.
    """
    if turn_count != max_turns or not transcript:
        return False
    return transcript[-1].get("type") == "user"


def run_native_writer_api(
    observations: Sequence[Observation],
    materialize_root: Path,
    *,
    client: Any,
    session: EvalSession,
    model: str = DEFAULT_ROLLOUT_MODEL,
) -> NativeWriterResult:
    """API-mode counterpart to :func:`run_native_writer` (issue
    athenaeum#1774): drives :func:`run_api_tool_loop` once per entry in
    *observations*, in order, all sharing the SAME fresh memory directory
    under *materialize_root* -- the same multi-day-store property
    :func:`run_native_writer`'s own docstring establishes, so a later
    session can see (and choose to update) what an earlier one wrote.

    Serves the harness's own `read`/`grep`/`write`/`edit`/`list` tools over
    *memory_dir*, every one of them confined by
    :func:`_resolve_under_memory_dir` -- see this section's own header
    comment for why. System prompt mirrors Claude Code's DOCUMENTED
    auto-memory writing behaviour (:func:`_native_writer_system_prompt`; see
    its own docstring for the fidelity gap against Claude Code's actual,
    closed-source prompt). Carries no :data:`REFERENCE_TAG_INSTRUCTION` --
    this arm produces memory files, not a graded answer (design doc §5: "The
    write-path sessions of Phase 2 are outside this contract").

    Uses :data:`_WRITER_API_LOOP_MAX_TURNS`, not the read arms'
    :data:`_API_LOOP_MAX_TURNS` -- see that constant's own docstring for
    why the writer needs a larger budget. Each session's
    ``turns_exhausted`` is set from :func:`_api_loop_turns_exhausted` when
    that budget was cut off mid-tool-use, so a report can distinguish "the
    model finished filing" from "the harness stopped it before it could."

    Returns the same :class:`NativeWriterResult` shape :func:`run_native_writer`
    returns (``mode="api"``, ``prompt_fidelity="reconstructed"`` -- see
    :attr:`NativeWriterResult.prompt_fidelity`'s own docstring for what that
    labels), so
    :func:`tests.evals.north_star_report.compute_write_path_stats` needs no
    change to consume either.
    """
    memory_dir = materialize_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    loaded_files: dict[str, str] = {}

    def _executor(name: str, tool_input: dict[str, Any]) -> str:
        if name == READ_TOOL_NAME:
            return _native_read_executor(memory_dir, tool_input, loaded=loaded_files)
        if name == GREP_TOOL_NAME:
            return _native_grep_executor(memory_dir, tool_input)
        if name == WRITE_TOOL_NAME:
            return _native_write_executor(memory_dir, tool_input)
        if name == EDIT_TOOL_NAME:
            return _native_edit_executor(memory_dir, tool_input)
        if name == LIST_TOOL_NAME:
            return _native_list_executor(memory_dir)
        return f"error: unknown tool {name!r}"

    tools = [
        _native_read_tool_schema(),
        _native_grep_tool_schema(),
        _native_write_tool_schema(),
        _native_edit_tool_schema(),
        _native_list_tool_schema(),
    ]
    system = _native_writer_system_prompt(memory_dir)

    sessions: list[NativeWriterSession] = []
    for i, observation in enumerate(observations):
        _answer, tool_calls, turn_tokens, turn_count, transcript = run_api_tool_loop(
            user_prompt=f"{_WRITER_PROMPT_PREFIX}{observation.body}",
            system=system,
            tools=tools,
            tool_executor=_executor,
            client=client,
            session=session,
            model=model,
            max_turns=_WRITER_API_LOOP_MAX_TURNS,
        )
        sessions.append(
            NativeWriterSession(
                session_index=i,
                observation_uid=observation.uid,
                tool_calls=tool_calls,
                turn_tokens=turn_tokens,
                turn_count=turn_count,
                transcript=transcript,
                turns_exhausted=_api_loop_turns_exhausted(
                    turn_count, _WRITER_API_LOOP_MAX_TURNS, transcript
                ),
            )
        )

    memory_files = {
        str(path.relative_to(memory_dir)): path.read_text(encoding="utf-8")
        for path in sorted(memory_dir.rglob("*"))
        if path.is_file()
    }
    return NativeWriterResult(
        sessions=sessions,
        memory_dir=memory_dir,
        memory_files=memory_files,
        mode="api",
        prompt_fidelity="reconstructed",
    )


def run_native_writer_dispatch(
    observations: Sequence[Observation],
    materialize_root: Path,
    *,
    mode: str = "api",
    client: Any | None = None,
    session: EvalSession | None = None,
    model: str = DEFAULT_ROLLOUT_MODEL,
    claude_binary: str = "claude",
    timeout: float = 120.0,
) -> NativeWriterResult:
    """Mode-switched entry point for the Phase 2 write path (issue
    athenaeum#1774), mirroring how :func:`run_probe_all_arms` resolves its
    own *mode* (design doc §4): ``"api"`` (default, matching every other
    arm's default in this module) drives :func:`run_native_writer_api` and
    requires *client* and *session*; ``"cli"`` calls :func:`run_native_writer`
    UNCHANGED -- byte-identical to calling it directly, no argument
    reshaping in between.

    This is the seam the CLI flags that will actually select Phase 2's write
    path (item N, athenaeum#1785, out of this issue's scope) dispatch
    through, the same way `north_star_cli.py`'s existing `--mode` flag
    already resolves `run_probe_all_arms(mode=...)`.
    """
    if mode not in ("cli", "api"):
        raise ValueError(f"unknown mode {mode!r}; expected 'cli' or 'api'")
    if mode == "cli":
        return run_native_writer(
            observations,
            materialize_root,
            claude_binary=claude_binary,
            model=model,
            timeout=timeout,
        )
    if client is None or session is None:
        raise ValueError("mode='api' requires both client and session")
    return run_native_writer_api(
        observations, materialize_root, client=client, session=session, model=model
    )


# ---------------------------------------------------------------------------
# Runner entrypoint — one probe, all eight arms
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
    mode: str = "api",
    pull_runner: Callable[..., RolloutRecord] | None = None,
    breadcrumb_context_fn: Callable[..., str] | None = None,
    breadcrumb_pull_runner: Callable[..., RolloutRecord] | None = None,
    native_index_runner: Callable[..., RolloutRecord] | None = None,
    native_grep_runner: Callable[..., RolloutRecord] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, RolloutRecord]:
    """Run ONE probe across all eight arms against a materialized corpus at
    *corpus_scale* (issue athenaeum#1522 AC2/AC3/AC4; issue athenaeum#1574
    added the two breadcrumb arms; issue athenaeum#1725 added the two
    native-memory arms).

    Builds exactly one :class:`~tests.evals.containment.GridCell` per arm via
    ``containment.build_grid("full", ...)`` — "full" is uncapped on the arms
    axis, so passing all eight ``Arm`` values with a single-element probe/
    corpus-scale/replicate list yields exactly ``len(ALL_ARMS)`` cells,
    reusing the SAME grid machinery a future multi-probe sweep would use
    rather than a bespoke loop.

    *client* and *pull_runner* are injectable seams (default to a real live
    client / :func:`run_pull`); *breadcrumb_context_fn* and
    *breadcrumb_pull_runner* are the SAME kind of seam for the two
    breadcrumb arms (default to :func:`build_push_breadcrumb_context` /
    :func:`run_push_breadcrumb_pull`); *native_index_runner* and
    *native_grep_runner* are the same kind of seam again, for the two native
    arms (default to :func:`run_native_index` / :func:`run_native_grep`) —
    so a caller, including the offline test suite, can supply stubs and
    exercise the arm-dispatch wiring without a network call or a subprocess
    spawn.

    *mode* (issue athenaeum#1733) selects the execution path for the four
    tool-using arms ONLY -- ``"api"`` (default, matching
    ``north_star_cli.py``'s own default so there is exactly ONE default
    across this module and its CLI driver, per Quine review) drives an
    Anthropic Messages API tool-use loop (see
    ``run_pull_api``/``run_push_breadcrumb_pull_api``/``run_native_index_api``/
    ``run_native_grep_api``, docs/design/native-memory-baseline.md §4) and
    requires *client* (no ``claude`` binary is ever invoked in this mode).
    ``"cli"`` spawns ``claude -p`` as it always has -- pass it explicitly for
    that behavior; every offline test in this suite that wants CLI-shaped
    dispatch (a ``materialize_root``/``claude_binary``-taking stub) now
    passes ``mode="cli"`` explicitly rather than relying on a default that
    used to be "cli" but no longer is.
    An explicitly passed ``pull_runner``/``breadcrumb_pull_runner``/
    ``native_index_runner``/``native_grep_runner`` always wins over *mode*'s
    default resolution -- exactly how these seams already behaved before
    *mode* existed, so no caller that already injects a stub needs to change.
    The four single-shot arms (NONE, PUSH_PAGES_UPPER_BOUND, PUSH_BREADCRUMB,
    ORACLE) are unaffected by *mode*: they always call *client* directly.
    """
    if mode not in ("cli", "api"):
        raise ValueError(f"unknown mode {mode!r}; expected 'cli' or 'api'")
    corpus = build_corpus(corpus_scale)
    probe = _find_probe(corpus, probe_id)
    wiki_root = corpus.materialize(materialize_root)
    cache_dir = materialize_root / "cache"
    hook_home = materialize_root / "hook_home"
    if search_backend != "keyword":
        get_backend(search_backend).build_index(wiki_root, cache_dir)
        # Issue athenaeum#1816: production keeps ONE cache dir backing BOTH
        # backends (see tests/evals/test_recall_covers_grep.py's
        # `scale_fixture` comment block for the same layout and rationale),
        # so the RRF hybrid block in `mcp_server.recall_search`
        # (``backend_name == "vector" and resolve_recall_hybrid(config)``,
        # DEFAULT ON) has an FTS5 index to fuse against. A vector dispatch
        # that built only the vector index measured a configuration nobody
        # ships -- every ``recall_search`` call fell back to vector-only
        # ranking and logged a warning naming this exact cache dir. Build
        # FTS5 second (into the SAME cache_dir, cheap relative to the
        # embedding pass just above) so a vector dispatch always has one.
        if search_backend == "vector":
            get_backend("fts5").build_index(wiki_root, cache_dir)
            if not fts5_index_available(cache_dir):
                # Should be unreachable -- build_index above either raises
                # on a real failure or leaves a usable index -- but a
                # silent no-op here would reproduce this exact issue with
                # no warning at all, so fail loudly rather than let the
                # grid run 720 cells of vector-only-ranking hybrid fusion
                # a second time.
                raise RuntimeError(
                    f"vector dispatch built no FTS5 index alongside the vector "
                    f"index at {cache_dir} -- hybrid fusion would silently "
                    "fall back to vector-only ranking for every recall call "
                    "in this group (issue athenaeum#1816)"
                )

    # Issue athenaeum#1761 item 4: the raw backend scores for THIS probe's
    # query, against the SAME index just built above -- a same-backend/
    # same-index approximation of what the shipped breadcrumb hook saw for
    # the same query, not its own internal ranking (the hook prints no
    # scores at all; see ``RolloutRecord.retrieval_hit_scores``'s
    # docstring). Computed once per probe, not once per arm: the score
    # landscape does not depend on which arm is about to run. Never raises
    # -- a backend query failure degrades to ``None`` (no scores recorded),
    # same fail-open shape the rest of this module already uses for a
    # missing binary or an empty index.
    try:
        retrieval_hit_scores: list[float] | None = [
            score
            for (_filename, _name, score) in get_backend(search_backend).query(
                probe.query, cache_dir, n=5, wiki_root=wiki_root
            )
        ]
    except Exception:  # noqa: BLE001 -- diagnostic-only, never fatal to a rollout
        retrieval_hit_scores = None

    cells: list[GridCell] = build_grid(
        "full",
        probes=[probe_id],
        arms=[arm.value for arm in ALL_ARMS],
        corpus_scales=[corpus_scale],
        replicates=[replicate],
    )

    resolved_client = client if client is not None else build_live_client()
    resolved_pull_runner = pull_runner if pull_runner is not None else (
        run_pull_api if mode == "api" else run_pull
    )
    resolved_breadcrumb_pull_runner = breadcrumb_pull_runner or (
        run_push_breadcrumb_pull_api if mode == "api" else run_push_breadcrumb_pull
    )
    resolved_native_index_runner = native_index_runner or (
        run_native_index_api if mode == "api" else run_native_index
    )
    resolved_native_grep_runner = native_grep_runner or (
        run_native_grep_api if mode == "api" else run_native_grep
    )

    records: dict[str, RolloutRecord] = {}
    for cell in cells:
        if should_stop is not None and should_stop():
            # Checked BETWEEN arms, not merely before the group (issue
            # athenaeum#1751). A concurrent grid runner trips its spend
            # ceiling on some other worker's cell; without this check the
            # overshoot is a whole group per worker -- up to len(ALL_ARMS)
            # cells each -- because this loop would run to completion.
            # With it the overshoot is at most the one arm already in
            # flight per worker.
            #
            # Raising, rather than returning the arms run so far, is what
            # keeps the caller's store consistent: resume granularity is
            # the whole group (every arm cell-key must be present), so a
            # partially-populated dict could only be appended as rows that
            # a resume would then append AGAIN, and ``ResultStore`` never
            # de-duplicates. The arms already run in this group are lost
            # spend either way -- a group that stops mid-way is re-run in
            # full on resume.
            raise SpendCeilingExceededError(
                f"stopping probe {probe_id!r} at corpus scale {corpus_scale!r} "
                f"after {len(records)} of {len(cells)} arms -- the run's spend "
                "ceiling tripped while this group was in flight"
            )
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
            if mode == "api":
                record = resolved_breadcrumb_pull_runner(
                    probe,
                    materialize_root,
                    hook_home,
                    cache_dir,
                    corpus_scale,
                    client=resolved_client,
                    session=session,
                    model=model,
                    search_backend=search_backend,
                    context_fn=breadcrumb_context_fn,
                    wiki_root=wiki_root,
                )
            else:
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
        elif arm is Arm.NATIVE_INDEX:
            # A dedicated subdirectory, NOT ``materialize_root`` itself:
            # NATIVE_INDEX and NATIVE_GREP each write their own
            # ``memory/``, ``claude-config/``, settings and mcp-config
            # files, and both run in the same ``run_probe_all_arms`` call —
            # sharing ``materialize_root`` between them would let one
            # arm's config/settings files clobber the other's.
            if mode == "api":
                record = resolved_native_index_runner(
                    probe,
                    materialize_root / "native_index",
                    corpus_scale,
                    client=resolved_client,
                    session=session,
                    model=model,
                )
            else:
                record = resolved_native_index_runner(
                    probe,
                    materialize_root / "native_index",
                    corpus_scale,
                    claude_binary=claude_binary,
                    model=model,
                )
        elif arm is Arm.NATIVE_GREP:
            if mode == "api":
                record = resolved_native_grep_runner(
                    probe,
                    materialize_root / "native_grep",
                    corpus_scale,
                    client=resolved_client,
                    session=session,
                    model=model,
                )
            else:
                record = resolved_native_grep_runner(
                    probe,
                    materialize_root / "native_grep",
                    corpus_scale,
                    claude_binary=claude_binary,
                    model=model,
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
            # This is the last remaining arm (PULL) by elimination — every
            # other member of ``ALL_ARMS`` is handled by an explicit branch
            # above.
            if mode == "api":
                record = resolved_pull_runner(
                    probe,
                    wiki_root,
                    cache_dir,
                    corpus_scale,
                    client=resolved_client,
                    session=session,
                    model=model,
                    search_backend=search_backend,
                )
            else:
                record = resolved_pull_runner(
                    probe,
                    materialize_root,
                    cache_dir,
                    corpus_scale,
                    claude_binary=claude_binary,
                    model=model,
                )
        record.retrieval_hit_scores = retrieval_hit_scores
        record.search_backend = search_backend
        records[arm.value] = record
    return records
