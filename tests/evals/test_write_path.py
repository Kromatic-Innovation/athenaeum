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
:class:`~tests.evals.harness.EvalSession`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.conftest import FakeLLMClient, make_llm_response, make_llm_usage
from tests.evals.corpus import Observation, ObservationStream
from tests.evals.harness import EvalSession
from tests.evals.north_star_report import compute_write_path_stats
from tests.evals.write_path import compile_observation_stream

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


def _stub_responder(**kwargs: Any) -> Any:
    """Route each ``messages.create`` call to a classify or create response
    for whichever fixture page its prompt mentions -- by CONTENT, not by
    call index, so this stays correct regardless of processing order.

    A classify call's tool/system prompt asks for JSON; this stub can't
    distinguish classify from create by shape alone, so it looks at what the
    call is FOR: a fresh page always needs a classify decision before a
    create, and the create call's prompt carries the classify tier's own
    entity_type framing. The one thing both calls have in common is the raw
    observation text (checked above) -- so the SAME classify JSON is legal
    to return for a call this stub cannot yet tell is create vs classify,
    because ``athenaeum.tiers`` re-derives the entity name from the same
    JSON list shape on the classify hop and only needs prose body text on
    the create hop. To keep the response shape valid on BOTH hops, this
    responder inspects the ``system``/``messages`` text for the literal
    marker the create-stage prompt always includes: the word "markdown" or
    "Write the full page" (present only on the create hop); everything else
    is treated as classify. Every response carries a non-zero
    ``usage`` (:func:`~tests.conftest.make_llm_usage`) so the driver's spend
    tracking (:class:`tests.evals.write_path._SpendTrackingClient`) has
    something real to tally.
    """
    usage = make_llm_usage(input_tokens=37, output_tokens=11)
    name = _page_name_of(kwargs.get("messages"))
    if name is None:
        # Not a fixture-page call (e.g. a post-compile contradiction/dedup
        # pass) -- a harmless empty JSON array parses cleanly wherever a
        # classify-shaped response is expected and is ignored by anything
        # that tolerates "no findings".
        return make_llm_response("[]", usage=usage)

    system_text = json.dumps(kwargs.get("system", ""))
    is_create_hop = bool(re.search(r"markdown|write the (full )?page|render", system_text, re.I))
    entry = next(p for p in _FIXTURE_PAGES if p[1] == name)
    _, entity_name, paragraph, token = entry
    if is_create_hop:
        return make_llm_response(f"# {entity_name}\n\n{paragraph} {token}.\n", usage=usage)
    return make_llm_response(
        json.dumps(
            [
                {
                    "name": entity_name,
                    "entity_type": "reference",
                    "tags": [],
                    "access": "internal",
                    "observations": f"{paragraph} {token}.",
                }
            ]
        ),
        usage=usage,
    )


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
    client = FakeLLMClient(responder=_stub_responder)

    store_files, write_cost = compile_observation_stream(
        stream, knowledge_root, client=client, model="claude-haiku-4-5"
    )

    assert store_files, "compile produced no wiki pages at all"
    corpus_text = "\n".join(store_files.values())
    for _, _, _, token in _FIXTURE_PAGES:
        assert token in corpus_text, f"planted token {token!r} missing from compiled store"

    assert write_cost.system == "athenaeum"
    assert write_cost.corpus_scale == "core"
    assert write_cost.input_tokens > 0
    assert write_cost.output_tokens > 0


def test_compile_observation_stream_output_is_consumable_by_write_path_stats(
    tmp_path: Path,
) -> None:
    """The existing token scanner (issue athenaeum#1726) must find every
    planted token in what this driver produces -- the acceptance criterion
    that makes this a real Phase 2 driver rather than a plausible-looking
    stub."""
    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    client = FakeLLMClient(responder=_stub_responder)

    store_files, _ = compile_observation_stream(
        stream, knowledge_root, client=client, model="claude-haiku-4-5"
    )
    stats = compute_write_path_stats("athenaeum", stream.scale, stream.observations, store_files)

    assert stats.pages_targeted == len(_FIXTURE_PAGES)
    assert stats.pages_written == len(_FIXTURE_PAGES)
    assert stats.answer_tokens_retained == stats.answer_tokens_total == len(_FIXTURE_PAGES)
    assert stats.observations_dropped == 0


def test_compile_observation_stream_records_spend_on_eval_session(tmp_path: Path) -> None:
    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    client = FakeLLMClient(responder=_stub_responder)
    session = EvalSession()

    _, write_cost = compile_observation_stream(
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


def test_compile_observation_stream_writes_athenaeum_yaml_pinning_model(tmp_path: Path) -> None:
    stream = _fixture_stream()
    knowledge_root = tmp_path / "knowledge"
    client = FakeLLMClient(responder=_stub_responder)

    compile_observation_stream(stream, knowledge_root, client=client, model="claude-haiku-4-5")

    import yaml

    config = yaml.safe_load((knowledge_root / "athenaeum.yaml").read_text())
    assert config["models"]["write"] == "claude-haiku-4-5"
