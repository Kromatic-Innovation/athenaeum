# SPDX-License-Identifier: Apache-2.0
"""Offline contract test for the Athenaeum-side Phase 2 write path (issue
athenaeum#1775).

UNMARKED — no ``pytest.mark.eval``/``live``/``embedding`` — so this runs in
the DEFAULT pytest selection, offline, with no credential and no network
dependency, the same discipline ``tests/evals/test_write_tier_compare_stub.py``
documents for its own harness proof (athenaeum#1139): "a harness that has
never executed is worthless" — this is the execution, against
:class:`tests.conftest.FakeLLMClient` (a canned response double), never the
network.

Exercises the FULL path issue athenaeum#1775's acceptance criteria name:
:meth:`~tests.evals.corpus.ObservationStream.materialize` writes the raw
files, :func:`athenaeum.librarian.run` (the real production compile
entrypoint, not a stub of it) turns them into wiki pages, the existing
token scanner (:func:`tests.evals.north_star_report.compute_write_path_stats`)
finds the planted ``answer_tokens`` in the result, and the compile's spend
lands on both the returned :class:`WriteCost` and an
:class:`~tests.evals.harness.EvalSession`. Plus the Quine-review follow-ups
(issue athenaeum#1775 PR review): the librarian's own exit code is captured
and typed-raised/returned rather than discarded, the stub's classify/create
hop routing is proven correct by response SHAPE and call sequence (not just
"the token showed up somewhere"), cache tokens agree between
:class:`WriteCost` and an :class:`EvalSession`, and the written
``athenaeum.yaml`` pins every model knob plus the provider.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from athenaeum.librarian import EXIT_GRACEFUL_PARTIAL, EXIT_LIBRARIAN_REFUSAL
from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.evals.corpus import Observation, ObservationStream
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import compute_write_path_stats
from tests.evals.write_path import (
    _MODEL_KNOBS,
    CompileOutcome,
    LibrarianCompileError,
    compile_observation_stream,
)

# Two synthetic pages, one observation each, each carrying its own planted
# token — small enough to be a "small fixture ObservationStream" (issue
# athenaeum#1775 AC) while still covering the two-call-per-page (classify,
# then create) shape a brand-new page takes through the librarian's
# entity-tier pipeline (mirrors ``tests/test_librarian.py``'s own
# ``TestRunIntegration`` fixed classify->create response-pair pattern).
_FIXTURE_PAGES: tuple[tuple[str, str, str, str], ...] = (
    # (page_uid, entity_name, paragraph, planted_token)
    ("page-alpha", "Fixture Alpha", "Fixture Alpha keeps its notes here.", "Cinderquill7"),
    ("page-beta", "Fixture Beta", "Fixture Beta keeps its notes here.", "Harrowvex9"),
)


def _fixture_stream() -> ObservationStream:
    observations = [
        Observation(
            uid=f"obs-{page_uid}-000",
            page_uid=page_uid,
            source="sessions",
            timestamp=f"2026010{i + 1}T000000Z",
            uuid8=f"{i:08d}",
            body=f"{name}: {paragraph} {token}.",
            answer_tokens=(token,),
        )
        for i, (page_uid, name, paragraph, token) in enumerate(_FIXTURE_PAGES)
    ]
    return ObservationStream(observations=observations, seed=1, scale="core")


def _page_name_of(messages: Any) -> str | None:
    """Recover the fixture page name from the raw observation text embedded
    in a classify/create call's user message, so the stub responder can
    answer any call order the librarian pipeline chooses rather than
    assuming a fixed sequence."""
    text = json.dumps(messages)
    for _, name, _, _ in _FIXTURE_PAGES:
        if name in text:
            return name
    return None


def _make_stub_responder(usage: Any = None) -> tuple[Any, list[tuple[str, str]]]:
    """Build a stub ``responder(**kwargs) -> response`` plus the call log it
    appends ``(entity_name, hop)`` to for every call, where ``hop`` is
    ``"classify"``, ``"create"``, or ``"other"`` (a call this stub cannot
    attribute to a fixture page, e.g. a post-compile contradiction/dedup
    pass).

    Routes by CONTENT (which fixture page the call's messages mention, and
    whether its system prompt carries the create-stage's own markdown/write-
    the-page framing), not by call index, so this stays correct regardless
    of processing order.

    Issue athenaeum#1775 Quine review, must-fix 2 — the two hops' response
    SHAPES are made deliberately distinguishable, not just their content:
    the create-hop response is a bare markdown page (starts with ``# ``, is
    NOT valid JSON) and the classify-hop response is a JSON array that does
    NOT contain the planted token (the token only ever appears in the
    create-hop page body). A misrouted call therefore fails LOUDLY — either
    the "shape" assertions in the tests below, or downstream in
    ``athenaeum.tiers`` itself (a classify call fed a bare markdown string
    where it expects JSON, or vice versa) — instead of silently passing a
    "token found somewhere in the store" assertion that a misroute could
    satisfy by accident (the pre-review version of this stub embedded the
    token in BOTH hops' responses, so that assertion could not tell a
    correct route from a misroute).
    """
    if usage is None:
        usage = make_llm_usage(input_tokens=37, output_tokens=11)
    call_log: list[tuple[str, str]] = []

    def _responder(**kwargs: Any) -> Any:
        name = _page_name_of(kwargs.get("messages"))
        if name is None:
            call_log.append(("<none>", "other"))
            # A harmless empty JSON array parses cleanly wherever a
            # classify-shaped response is expected and is ignored by
            # anything that tolerates "no findings".
            return make_llm_response("[]", usage=usage)

        system_text = json.dumps(kwargs.get("system", ""))
        is_create_hop = bool(
            re.search(r"markdown|write the (full )?page|render", system_text, re.I)
        )
        entry = next(p for p in _FIXTURE_PAGES if p[1] == name)
        _, entity_name, paragraph, token = entry

        if is_create_hop:
            call_log.append((entity_name, "create"))
            return make_llm_response(f"# {entity_name}\n\n{paragraph} {token}.\n", usage=usage)

        call_log.append((entity_name, "classify"))
        # Deliberately NO planted token in the classify-hop response (must-
        # fix 2) -- only the create-hop page body carries it.
        return make_llm_response(
            json.dumps(
                [
                    {
                        "name": entity_name,
                        "entity_type": "reference",
                        "tags": [],
                        "access": "internal",
                        "observations": paragraph,
                    }
                ]
            ),
            usage=usage,
        )

    return _responder, call_log


def test_stub_responder_hop_shapes_are_distinguishable_and_token_isolated() -> None:
    """Direct, unit-level proof of the must-fix-2 guard: the classify and
    create hop responses cannot be confused with each other, and the
    planted token appears in the create-hop response ONLY."""
    responder, call_log = _make_stub_responder()
    _, entity_name, paragraph, token = _FIXTURE_PAGES[0]
    base_messages = [{"role": "user", "content": f"{entity_name}: {paragraph} {token}."}]

    classify_response = responder(messages=base_messages, system="Classify this observation.")
    create_response = responder(messages=base_messages, system="Write the full page in markdown.")

    classify_text = classify_response.content[0].text
    create_text = create_response.content[0].text

    assert create_text.startswith("# "), "create-hop response must be a markdown page"
    with pytest.raises(json.JSONDecodeError):
        json.loads(create_text)

    parsed = json.loads(classify_text)  # must not raise
    assert isinstance(parsed, list)
    assert token not in classify_text, "classify-hop response must not leak the planted token"

    assert call_log == [(entity_name, "classify"), (entity_name, "create")]


def test_compile_observation_stream_materializes_raw_files_before_compiling(
    tmp_path: Path,
) -> None:
    """The stream must land under ``raw/<source>/`` in the discoverable
    layout (issue athenaeum#1775 AC) BEFORE the compile consumes it -- a
    successful compile retires (deletes) each processed raw file as part of
    the librarian's normal move-then-retire discipline, so this asserts the
    materialize step itself, via the same seeding
    :func:`~tests.evals.write_path.compile_observation_stream` performs,
    rather than the post-compile raw tree (which is legitimately empty)."""
    from tests.evals.write_path import _ensure_git_repo, _seed_knowledge_root

    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    _seed_knowledge_root(knowledge_root)
    _ensure_git_repo(knowledge_root)

    stream.materialize(knowledge_root)

    raw_sessions = knowledge_root / "raw" / "sessions"
    materialized = {p.name for p in raw_sessions.glob("*.md")}
    assert materialized == {obs.filename for obs in stream.observations}


def test_compile_observation_stream_produces_a_page_per_token_bearing_page(
    tmp_path: Path,
) -> None:
    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    responder, call_log = _make_stub_responder()
    client = FakeLLMClient(responder=responder)

    store_files, write_cost, outcome = compile_observation_stream(
        # ``session_size=1`` pins the ONE-FILE-PER-OBSERVATION shape (issue
        # athenaeum#1824): this fixture's two observations are about two
        # DIFFERENT entities, and ``_make_stub_responder`` routes a call by
        # the single fixture page its messages mention, so bundling them
        # into one raw file would hand the stub a create hop naming two
        # pages and make the routing assertions below meaningless. The
        # bundled production shape is covered by its own test
        # (``...bundles_observations_into_one_raw_file``) on a stream where
        # every observation is about the same entity.
        stream,
        knowledge_root,
        client=client,
        model="claude-haiku-4-5",
        session_size=1,
    )

    assert outcome.exit_code == 0
    assert outcome.partial is False
    # Issue athenaeum#1824: a window sized to the input leaves nothing
    # deferred, so this run's numbers are a result rather than a floor.
    assert outcome.deferred_raw_files == 0

    assert store_files, "compile produced no wiki pages at all"
    corpus_text = "\n".join(store_files.values())
    for _, _, _, token in _FIXTURE_PAGES:
        assert token in corpus_text, f"planted token {token!r} missing from compiled store"

    assert write_cost.system == "athenaeum"
    assert write_cost.corpus_scale == "core"
    assert write_cost.input_tokens > 0
    assert write_cost.output_tokens > 0

    # Must-fix 2: the token showing up in the store is not, by itself,
    # proof the routing was correct (a misroute that echoed the token back
    # on the classify hop could satisfy the assertion above too) -- prove
    # the actual call SEQUENCE was classify-then-create for each page.
    for _, entity_name, _, _ in _FIXTURE_PAGES:
        hops = [hop for name, hop in call_log if name == entity_name]
        assert hops == [
            "classify",
            "create",
        ], f"expected classify-then-create for {entity_name!r}, got {hops}"


def test_compile_observation_stream_output_is_consumable_by_write_path_stats(
    tmp_path: Path,
) -> None:
    """The existing token scanner (issue athenaeum#1726) must find every
    planted token in what this driver produces -- the acceptance criterion
    that makes this a real Phase 2 driver rather than a plausible-looking
    stub."""
    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    responder, _ = _make_stub_responder()
    client = FakeLLMClient(responder=responder)

    store_files, _write_cost, _outcome = compile_observation_stream(
        stream, knowledge_root, client=client, model="claude-haiku-4-5", session_size=1
    )
    stats = compute_write_path_stats("athenaeum", stream.scale, stream.observations, store_files)

    assert stats.pages_targeted == len(_FIXTURE_PAGES)
    assert stats.pages_written == len(_FIXTURE_PAGES)
    assert stats.answer_tokens_retained == stats.answer_tokens_total == len(_FIXTURE_PAGES)
    assert stats.observations_dropped == 0


def test_compile_observation_stream_bundles_observations_into_one_raw_file(
    tmp_path: Path,
) -> None:
    """Production intake shape (issue athenaeum#1824): the driver's default
    ``session_size`` bundles several observations into ONE raw file, the way
    Claude's auto-memory writes one file per session, instead of the
    one-file-per-observation shape the first Phase 2 smoke run used.

    That shape difference is the whole diagnosis: the librarian's
    ``max_files`` window is counted in FILES, so a stream materialised as
    hundreds of singletons overflowed it and had 163 of 213 files deferred
    to a later night (run 35292686290,
    ``docs/measurements/write-path-retention-2026-09-18.md``). Uses a
    single-entity stream so the stub's page routing stays unambiguous.
    """
    from tests.evals.write_path import _ensure_git_repo, _seed_knowledge_root

    page_uid, name, paragraph, token = _FIXTURE_PAGES[0]
    observations = [
        Observation(
            uid=f"obs-{page_uid}-{i:03d}",
            page_uid=page_uid,
            source="sessions",
            timestamp=f"2026020{i + 1}T000000Z",
            uuid8=f"{i:08d}",
            body=f"{name}: {paragraph} {token}.",
            answer_tokens=(token,),
        )
        for i in range(4)
    ]
    stream = ObservationStream(observations=observations, seed=1, scale="core")

    knowledge_root = tmp_path / "knowledge"
    _seed_knowledge_root(knowledge_root)
    _ensure_git_repo(knowledge_root)
    stream.materialize(knowledge_root, session_size=4)

    raw_files = sorted((knowledge_root / "raw" / "sessions").glob("*.md"))
    assert len(raw_files) == 1, "four observations must bundle into one session file"
    assert raw_files[0].name == observations[0].filename, (
        "a bundle is named after its EARLIEST observation, so the file still sorts "
        "by when the session started"
    )
    bundled = raw_files[0].read_text(encoding="utf-8")
    for obs in observations:
        assert obs.body in bundled

    # And the bundling knob is the only thing that changed: the same stream
    # at session_size=1 lands as four files.
    singleton_root = tmp_path / "singleton"
    _seed_knowledge_root(singleton_root)
    _ensure_git_repo(singleton_root)
    stream.materialize(singleton_root, session_size=1)
    assert len(sorted((singleton_root / "raw" / "sessions").glob("*.md"))) == 4


def test_compile_observation_stream_records_spend_on_eval_session(tmp_path: Path) -> None:
    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    responder, _ = _make_stub_responder()
    client = FakeLLMClient(responder=responder)
    session = EvalSession()

    _store_files, write_cost, _outcome = compile_observation_stream(
        stream,
        knowledge_root,
        client=client,
        model="claude-haiku-4-5",
        session=session,
    )

    assert session.input_tokens > 0
    assert session.output_tokens > 0
    assert session.input_tokens == write_cost.input_tokens
    assert session.output_tokens == write_cost.output_tokens


def test_compile_observation_stream_agrees_with_session_on_cached_tokens(tmp_path: Path) -> None:
    """Should-fix 1: ``WriteCost``'s input/output totals and an
    ``EvalSession``'s four-counter totals must be read from the SAME fields
    on the SAME response so they stay in agreement even when a response
    also carries cache tokens."""
    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    responder, _ = _make_stub_responder(
        usage=make_llm_usage(
            input_tokens=37,
            output_tokens=11,
            cache_creation_input_tokens=5,
            cache_read_input_tokens=3,
        )
    )
    client = FakeLLMClient(responder=responder)
    session = EvalSession()

    _store_files, write_cost, _outcome = compile_observation_stream(
        stream, knowledge_root, client=client, model="claude-haiku-4-5", session=session
    )

    assert session.cache_creation_input_tokens > 0
    assert session.cache_read_input_tokens > 0
    assert session.input_tokens == write_cost.input_tokens
    assert session.output_tokens == write_cost.output_tokens


def test_compile_observation_stream_writes_athenaeum_yaml_pinning_every_model_knob(
    tmp_path: Path,
) -> None:
    """Should-fix 2: every model knob a full compile can reach
    (:data:`tests.evals.write_path._MODEL_KNOBS`) is pinned to the caller's
    model, and the provider is pinned to ``api`` so an ambient
    ``ATHENAEUM_LLM_PROVIDER=claude-cli`` cannot route a call site around
    the patched ``anthropic.Anthropic`` client."""
    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    responder, _ = _make_stub_responder()
    client = FakeLLMClient(responder=responder)

    compile_observation_stream(stream, knowledge_root, client=client, model="claude-haiku-4-5")

    import yaml

    config = yaml.safe_load((knowledge_root / "athenaeum.yaml").read_text())
    assert _MODEL_KNOBS, "test setup bug: _MODEL_KNOBS is empty"
    for knob in _MODEL_KNOBS:
        assert config["models"][knob] == "claude-haiku-4-5"
    assert config["llm"]["provider"] == "api"


# ---------------------------------------------------------------------------
# Must-fix 1 -- athenaeum.librarian.run's exit code must not be discarded.
# librarian_run itself is monkeypatched so these are pure unit tests of the
# exit-code branch in compile_observation_stream, independent of whether a
# real compile happens to produce that code in this sandbox.
# ---------------------------------------------------------------------------


def test_compile_observation_stream_raises_on_error_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tests.evals.write_path.librarian_run", lambda **kwargs: 1)
    responder, _ = _make_stub_responder()
    client = FakeLLMClient(responder=responder)

    with pytest.raises(LibrarianCompileError, match="exited 1"):
        compile_observation_stream(
            _fixture_stream(), tmp_path / "knowledge", client=client, model="claude-haiku-4-5"
        )


def test_compile_observation_stream_raises_on_refusal_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tests.evals.write_path.librarian_run", lambda **kwargs: EXIT_LIBRARIAN_REFUSAL
    )
    responder, _ = _make_stub_responder()
    client = FakeLLMClient(responder=responder)

    with pytest.raises(LibrarianCompileError, match=f"exited {EXIT_LIBRARIAN_REFUSAL}"):
        compile_observation_stream(
            _fixture_stream(), tmp_path / "knowledge", client=client, model="claude-haiku-4-5"
        )


def test_compile_observation_stream_returns_partial_outcome_on_graceful_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 75 (``EXIT_GRACEFUL_PARTIAL``, a deadline trip) must NOT raise --
    the compile made real, partial progress and the caller should still get
    its (possibly incomplete) store back, with ``CompileOutcome.partial``
    naming the fact."""
    monkeypatch.setattr(
        "tests.evals.write_path.librarian_run", lambda **kwargs: EXIT_GRACEFUL_PARTIAL
    )
    responder, _ = _make_stub_responder()
    client = FakeLLMClient(responder=responder)

    store_files, _write_cost, outcome = compile_observation_stream(
        _fixture_stream(), tmp_path / "knowledge", client=client, model="claude-haiku-4-5"
    )

    assert isinstance(store_files, dict)
    assert isinstance(outcome, CompileOutcome)
    assert outcome.exit_code == EXIT_GRACEFUL_PARTIAL
    assert outcome.partial is True
