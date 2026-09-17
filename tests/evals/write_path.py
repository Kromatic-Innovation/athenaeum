# SPDX-License-Identifier: Apache-2.0
"""Athenaeum-side Phase 2 write path driver (issue athenaeum#1775).

Per eval-wave-2-spec.md §6.2: there is no Athenaeum-side compile driver at
all today — nothing feeds an :class:`~tests.evals.corpus.ObservationStream`
through :mod:`athenaeum.intake`/:mod:`athenaeum.tiers` to produce the
librarian's store, so the native-side write path
(``tests.evals.rollout.run_native_writer``/``run_native_writer_api``) has no
Athenaeum-side counterpart to compare against
(docs/design/native-memory-baseline.md §5 "Phase 2"). This module is that
counterpart: :func:`compile_observation_stream` materialises a stream into
``knowledge_root/raw/<source>/`` (:meth:`~tests.evals.corpus.ObservationStream.materialize`)
and runs it through :func:`athenaeum.librarian.run` — the SAME production
entrypoint ``athenaeum run``/the nightly scheduler use, not a reimplementation
of Tier 1/2/3 — so the compiled wiki this produces is the real thing, not an
eval-only approximation.

Deliberately a NEW module, not an addition to ``tests/evals/rollout.py``:
that module owns *arms* (the native writer's own session runners); a compile
run is not an arm, it has no scorer, and it feeds ``compute_write_path_stats``
directly instead of a ``RolloutRecord`` (issue athenaeum#1775 proposal).

Spend capture (issue athenaeum#1775 task brief — "records the librarian's
spend on an EvalSession-compatible record"): :func:`athenaeum.librarian.run`
constructs its own ``anthropic.Anthropic(...)`` client internally (the same
production call sites :mod:`athenaeum.tiers` uses) rather than accepting one
as a parameter, so :class:`_SpendTrackingClient` wraps the CALLER-supplied
*client* and is installed via ``unittest.mock.patch("anthropic.Anthropic",
...)`` for the duration of the compile — the identical seam
``tests/test_librarian.py``'s own ``TestRunIntegration`` uses to run the real
pipeline against a mocked/stubbed client with no network. Every
``messages.create`` response observed this way is both tallied locally (into
the returned :class:`~tests.evals.north_star_report.WriteCost`) and, when a
:class:`~tests.evals.harness.EvalSession` is supplied, folded into its
running per-model totals via
:meth:`~tests.evals.harness.EvalSession.observe_response` — the same
accounting every other eval layer's live turn uses.

Out of scope (issue athenaeum#1775): CLI flags / ``evals.yml`` wiring
(athenaeum#1785/#1786), the native-side writer (athenaeum#1774, item L), and
any change to ``athenaeum.intake``/``athenaeum.tiers`` themselves.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from athenaeum.librarian import EXIT_GRACEFUL_PARTIAL, EXIT_LIBRARIAN_REFUSAL
from athenaeum.librarian import run as librarian_run
from tests.evals.corpus import ObservationStream
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import WriteCost

__all__ = ["CompileOutcome", "LibrarianCompileError", "compile_observation_stream"]


class LibrarianCompileError(RuntimeError):
    """Raised when :func:`athenaeum.librarian.run` returns a
    non-recoverable exit code during :func:`compile_observation_stream`
    (issue athenaeum#1775 Quine review, must-fix 1): ``1`` (error) or
    :data:`~athenaeum.librarian.EXIT_LIBRARIAN_REFUSAL` (``3`` — a
    zero-progress DEGRADED refusal, docs/reference/exit-codes.md). Deliberately
    NOT raised for :data:`~athenaeum.librarian.EXIT_GRACEFUL_PARTIAL` (``75``
    — a deadline trip): that run made real, partial progress and the
    resulting store is still valid to measure; see
    :class:`CompileOutcome.partial`."""


@dataclasses.dataclass(frozen=True)
class CompileOutcome:
    """The librarian compile run's own outcome (issue athenaeum#1775 Quine
    review, must-fix 1), returned alongside :class:`WriteCost` rather than
    folded onto it: :class:`~tests.evals.north_star_report.WriteCost` is a
    frozen dataclass owned by a sibling lane (issue athenaeum#1776/S3) and
    this issue is explicitly out of scope to edit it (see this module's
    docstring's "Out of scope" note) — a small wrapper carries the same
    information without an adapter, since neither
    ``compute_write_path_stats`` nor ``build_report`` needs to see it.

    ``exit_code`` is :func:`athenaeum.librarian.run`'s own return value
    (0 success, 1 error, 75 :data:`~athenaeum.librarian.EXIT_GRACEFUL_PARTIAL`,
    3 :data:`~athenaeum.librarian.EXIT_LIBRARIAN_REFUSAL`) — 1 and 3 are
    raised as :class:`LibrarianCompileError` instead of reaching here.
    ``partial`` is ``True`` only for a 75 (deadline-tripped) run: the
    compile committed real progress but did not finish everything it was
    given, so a caller comparing write costs across systems may want to
    flag or exclude a partial cell rather than average it in silently."""

    exit_code: int
    partial: bool


def _usage_of(response: Any) -> dict[str, int]:
    """Extract the four token counters athenaeum's spend ledger reads,
    replicating ``tests.evals.harness._cache_usage_from_response``'s exact
    field set (kept local here rather than imported so this module never
    reaches into that module's private helper) — so :class:`WriteCost`'s
    input/output totals and an :class:`~tests.evals.harness.EvalSession`'s
    four-counter totals are computed from the SAME fields on the SAME
    response and stay in agreement even when that response also carries
    cache tokens (issue athenaeum#1775 Quine review, should-fix 1)."""
    usage = getattr(response, "usage", None)

    def _get(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return {
        "input_tokens": _get("input_tokens"),
        "output_tokens": _get("output_tokens"),
        "cache_creation_input_tokens": _get("cache_creation_input_tokens"),
        "cache_read_input_tokens": _get("cache_read_input_tokens"),
    }


class _SpendTrackingClient:
    """Wraps *inner* (any ``client.messages.create(**kwargs)``-shaped
    object — a real ``anthropic.Anthropic`` instance or a test double) so
    every call the librarian compile makes through it is tallied for
    :class:`~tests.evals.north_star_report.WriteCost`, and, when *session*
    is given, also observed into it (module docstring above)."""

    def __init__(self, inner: Any, *, session: EvalSession | None, model: str) -> None:
        self._inner = inner
        self._session = session
        self._model = model
        self.messages = self
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0

    def create(self, **kwargs: Any) -> Any:
        response = self._inner.messages.create(**kwargs)
        self.calls += 1
        usage = _usage_of(response)
        self.input_tokens += usage["input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.cache_creation_input_tokens += usage["cache_creation_input_tokens"]
        self.cache_read_input_tokens += usage["cache_read_input_tokens"]
        if self._session is not None:
            self._session.observe_response(str(kwargs.get("model") or self._model), response)
        return response


def _seed_knowledge_root(knowledge_root: Path) -> None:
    """Create the minimal ``wiki/_schema`` + ``raw/`` tree
    :func:`athenaeum.librarian.run` needs to compile at all — the same three
    schema tables ``tests/test_librarian.py``'s own
    ``TestRunIntegration._seed_knowledge_root`` seeds for its own real
    ``run()`` integration coverage, reused here rather than invented fresh
    (issue athenaeum#1775: "the production call sites", not an eval-only
    shortcut). Never overwrites a schema file a caller already seeded."""
    wiki = knowledge_root / "wiki"
    schema = wiki / "_schema"
    schema.mkdir(parents=True, exist_ok=True)
    if not (schema / "types.md").exists():
        (schema / "types.md").write_text(
            "# Types\n\n| Type |\n|------|\n| person |\n| company |\n| reference |\n"
        )
    if not (schema / "tags.md").exists():
        (schema / "tags.md").write_text("# Tags\n\n| Tag |\n|-----|\n| active |\n")
    if not (schema / "access-levels.md").exists():
        (schema / "access-levels.md").write_text("# Access\n\n| Level |\n|-------|\n| internal |\n")
    (knowledge_root / "raw").mkdir(parents=True, exist_ok=True)


def _ensure_git_repo(knowledge_root: Path) -> None:
    """Init + commit *knowledge_root* as a git repo if it is not one already
    (issue athenaeum#1775: same production shape, not an eval-only shortcut).
    :func:`athenaeum.librarian.run` refuses to run at all without a writable
    git repo -- its pre-processing snapshot is load-bearing for raw-file
    recovery. Committed BEFORE :meth:`~tests.evals.corpus.ObservationStream.materialize`
    writes the raw observation files, so those land as uncommitted changes
    for ``run()``'s own pre-processing snapshot to see -- the same ordering
    ``tests/test_librarian.py``'s ``TestRunIntegration._seed_knowledge_root``
    uses ("Drop the raw intake file post-commit so it is an uncommitted
    change when run() takes its pre-processing snapshot.")."""
    if (knowledge_root / ".git").exists():
        return

    def _run(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(knowledge_root),
            capture_output=True,
            text=True,
            check=True,
        )

    _run("init", "-q", "-b", "main")
    _run("config", "user.email", "eval-write-path@example.com")
    _run("config", "user.name", "Eval Write Path")
    _run("add", "-A")
    _run("commit", "-q", "-m", "seed: knowledge root schema (eval write path)")


# Every model knob athenaeum's ``models.<knob>`` yaml block resolves through
# :func:`athenaeum.config.resolve_model` (or, for ``resolve``, the legacy-key
# variant :func:`athenaeum.resolutions._get_model` layers on top of the same
# helper) that a full :func:`athenaeum.librarian.run` compile can reach:
# ``write`` (:mod:`athenaeum.tiers` tier3 create/merge — also the write-tier-
# compare precedent's own knob), ``classify`` (:mod:`athenaeum.tiers` tier2,
# and :mod:`athenaeum.contradictions`, which deliberately reuses the classify
# knob), ``resolve`` (:mod:`athenaeum.resolutions`), ``topic``
# (:mod:`athenaeum.query_topics`), and ``rule_proposals``
# (:mod:`athenaeum.rule_proposals`). Pinning all five to the SAME *model* is
# what makes a compile run's spend attributable to one model, matching
# ``tests/evals/tier_compare.py``'s single-model-per-run discipline (issue
# athenaeum#1775 Quine review, should-fix 2). ``athenaeum.wiki_dedupe``/
# ``athenaeum.merge`` call no model of their own (pure similarity/embedding),
# so they need no knob here.
_MODEL_KNOBS: tuple[str, ...] = ("write", "classify", "resolve", "topic", "rule_proposals")


def _write_athenaeum_yaml(knowledge_root: Path, model: str) -> Path:
    """Write a real ``athenaeum.yaml`` pinning every model knob
    (:data:`_MODEL_KNOBS`) a full compile can reach to *model*, via
    ``models.<knob>`` — the SAME yaml precedence layer
    :func:`athenaeum.config.resolve_model` reads in production (env > yaml >
    default), not a ``config={...}`` dict shortcut threaded past ``run()``
    (which accepts no such parameter). Also pins ``llm.provider: api``
    (:func:`athenaeum.provider.resolve_provider`'s own yaml key) so an
    ambient ``ATHENAEUM_LLM_PROVIDER=claude-cli`` override cannot route a
    call site through :func:`athenaeum.provider.build_llm_client`'s
    subscription/CLI backend instead of ``anthropic.Anthropic`` — the only
    constructor :func:`compile_observation_stream` patches, so any other
    backend would silently escape the caller-supplied *client* entirely
    (issue athenaeum#1775 Quine review, should-fix 2). Overwritten on every
    call so a re-run always reflects the *model* the caller asked for."""
    path = knowledge_root / "athenaeum.yaml"
    config: dict[str, Any] = {
        "models": dict.fromkeys(_MODEL_KNOBS, model),
        "llm": {"provider": "api"},
    }
    path.write_text(yaml.safe_dump(config, sort_keys=True))
    return path


def _read_wiki_store(wiki_root: Path) -> dict[str, str]:
    """Return ``{relpath: text}`` for every compiled wiki page under
    *wiki_root*, excluding ``_schema``/other underscore-prefixed bookkeeping
    trees — the shape
    :func:`tests.evals.north_star_report.compute_write_path_stats` and
    :func:`~tests.evals.north_star_report.build_report` accept as
    *store_files* (both do a content-only substring scan; neither inspects
    filenames or frontmatter, per ``compute_write_path_stats``'s own
    docstring)."""
    store: dict[str, str] = {}
    if not wiki_root.is_dir():
        return store
    for path in sorted(wiki_root.rglob("*.md")):
        rel = path.relative_to(wiki_root)
        if any(part.startswith("_") for part in rel.parts):
            continue
        store[str(rel)] = path.read_text(encoding="utf-8")
    return store


def compile_observation_stream(
    stream: ObservationStream,
    knowledge_root: Path,
    *,
    client: Any,
    model: str,
    session: EvalSession | None = None,
    run_kwargs: dict[str, Any] | None = None,
) -> tuple[dict[str, str], WriteCost, CompileOutcome]:
    """Compile *stream* through the real librarian pipeline and return the
    resulting wiki store, its :class:`~tests.evals.north_star_report.WriteCost`,
    and the compile's own :class:`CompileOutcome`
    (issue athenaeum#1775, eval-wave-2-spec.md §6.2).

    This is the Athenaeum half of the Phase 2 write-path comparison
    (design doc §5): the SAME observation stream the native writer consumes
    goes in here too, compiled pages come out, and the compile's own spend is
    recorded — so both systems' write paths are measured on identical input.

    *knowledge_root* is seeded (or reused, if it already carries a
    ``wiki/_schema`` tree and a ``.git`` repo) with the minimal schema and
    git history the librarian needs, then *stream* is materialised into
    ``knowledge_root/raw/<source>/`` and compiled via
    :func:`athenaeum.librarian.run` — the production entrypoint, not a
    reimplementation of Tier 1/2/3. *client* is installed for the duration of
    the compile by patching ``anthropic.Anthropic`` (mirrors
    ``tests/test_librarian.py``'s own ``TestRunIntegration`` pattern), so a
    caller may pass either a real ``anthropic.Anthropic`` instance (a live
    Phase 2 grid run) or an offline stub/fake (this module's own
    ``--dry-run``-style contract test) with no other code path changing
    between the two. ``run_kwargs`` is forwarded verbatim to ``run()``; note
    that ``batch_mode=True`` is UNSUPPORTED here — :class:`_SpendTrackingClient`
    only wraps ``client.messages.create``, not the Anthropic Message Batches
    API a batch-mode compile would use instead, so a batched run's spend
    would silently go untracked.

    Returns ``(store_files, write_cost, outcome)``: *store_files* is
    ``{relpath: text}`` over the compiled wiki (excluding ``_schema``),
    directly consumable by ``compute_write_path_stats(store_files=...)``;
    *write_cost* is a :class:`WriteCost` with ``system="athenaeum"``,
    ``corpus_scale=stream.scale``, and the input/output token totals the
    compile spent, directly consumable by ``build_report(write_costs=...)``;
    *outcome* is the compile's own :class:`CompileOutcome` (exit code +
    whether it was a partial/deadline-tripped run). Raises
    :class:`LibrarianCompileError` if the compile exits ``1`` (error) or
    :data:`~athenaeum.librarian.EXIT_LIBRARIAN_REFUSAL` (``3``, a
    zero-progress refusal) — see :class:`CompileOutcome`'s docstring for the
    full exit-code contract.
    """
    knowledge_root = Path(knowledge_root)
    _seed_knowledge_root(knowledge_root)
    _ensure_git_repo(knowledge_root)
    stream.materialize(knowledge_root)
    _write_athenaeum_yaml(knowledge_root, model)

    tracking = _SpendTrackingClient(client, session=session, model=model)

    # A dummy key only when the environment carries none at all — the
    # patched ``anthropic.Anthropic`` constructor below always returns
    # *tracking* regardless of the key's value, but some call sites resolve
    # (and validate the presence of) ``ANTHROPIC_API_KEY`` before ever
    # constructing the client. Never overwrites a real operator key.
    had_key = "ANTHROPIC_API_KEY" in os.environ
    if not had_key:
        os.environ["ANTHROPIC_API_KEY"] = "eval-write-path-compile-not-a-real-key"

    run_options: dict[str, Any] = {
        "live_session_guard": False,
        "install_signal_handlers": False,
    }
    run_options.update(run_kwargs or {})

    try:
        with patch("anthropic.Anthropic", lambda **kwargs: tracking):
            exit_code = librarian_run(
                raw_root=knowledge_root / "raw",
                wiki_root=knowledge_root / "wiki",
                knowledge_root=knowledge_root,
                **run_options,
            )
    finally:
        if not had_key:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    if exit_code in (1, EXIT_LIBRARIAN_REFUSAL):
        raise LibrarianCompileError(
            f"athenaeum.librarian.run exited {exit_code} during "
            "compile_observation_stream (1=error, "
            f"{EXIT_LIBRARIAN_REFUSAL}=EXIT_LIBRARIAN_REFUSAL, a "
            "zero-progress refusal) — see docs/reference/exit-codes.md"
        )

    store_files = _read_wiki_store(knowledge_root / "wiki")
    write_cost = WriteCost(
        system="athenaeum",
        corpus_scale=stream.scale,
        input_tokens=tracking.input_tokens,
        output_tokens=tracking.output_tokens,
    )
    outcome = CompileOutcome(exit_code=exit_code, partial=exit_code == EXIT_GRACEFUL_PARTIAL)
    return store_files, write_cost, outcome
