# SPDX-License-Identifier: Apache-2.0
"""Raw-observation write-path round trip (issue athenaeum#1726 AC2).

**What this proves, precisely.** The generator's ground truth survives the
librarian's tiering/merge WIRING: an observation stream generated from one
core page's own ``answer_tokens`` (:func:`tests.evals.corpus.
generate_page_observations`), fed one file at a time through
``athenaeum.librarian.process_one`` (the real Tier 0/1/2/3 chain, the exact
seam ``tests/evals/test_attachment_eval.py`` already grades), compiles into a
wiki page that still carries every one of those planted tokens.

**What this does NOT prove.** Whether a REAL model, given these
observations, would choose to retain the same facts. This lane has no live
model backend here (``ANTHROPIC_API_KEY`` is unset; ``claude-cli`` also has
no session) — AC2's own text asks for "recorded fixtures", but authoring a
fixture by hand would replay this test's own words back at itself, which is
indistinguishable from evidence and exactly what recorded fixtures exist to
prevent measuring. Instead this test drives the classify/write tiers with a
DETERMINISTIC SCRIPTED CLIENT, the same established idiom as
``tests/test_batch_mode.py``'s ``_scripted_responder`` and
``tests/test_person_registry.py``'s ``_FakeClient`` — credential-free,
reproducible, and honest about being a stand-in for a model's classify/write
JUDGMENT rather than a measurement of it. The classify responder here always
resolves every observation to the ONE page it was generated from (it does
not have to guess, because it is not exercising classification quality);
what it cannot fake is whether the librarian's ACTUAL merge/write plumbing
(anchored-ops application, frontmatter round-trip, Tier 1 name matching)
drops a token on the way to disk — which is exactly what this test checks.

Measuring a real model's retention against this same observation stream is
the live grid run design doc §5 describes, and it is manual-dispatch only
(AC5) — never something an offline suite can assert.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from athenaeum.intake import discover_raw_files
from athenaeum.librarian import process_one
from athenaeum.models import EntityIndex, TokenUsage, parse_frontmatter
from tests.evals.corpus import (
    Observation,
    Page,
    generate_page_observations,
    load_core_pages,
    load_probes,
)

VALID_ACCESS = ["open", "internal", "confidential", "personal"]


class _ScriptedClient:
    """Minimal anthropic-shaped double: ``client.messages.create(**params)``
    delegates to *responder*. Same ad-hoc-``_FakeClient`` idiom as
    ``tests/test_person_registry.py``/``tests/test_batch_mode.py`` (issue
    athenaeum#554's L11 note: left ad-hoc rather than repointed at the
    shared ``tests.conftest.FakeLLMClient`` double, since every one of these
    per-test doubles wants a slightly different constructor shape)."""

    def __init__(self, responder: Callable[[dict[str, Any]], str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self
        self._responder = responder

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        response = MagicMock()
        response.content = [MagicMock(text=self._responder(params))]
        return response


def _round_trip_page() -> tuple[Page, str, tuple[str, ...]]:
    """The one core page + probe this test round-trips: ``policy-pto``, a
    ``single_hop`` probe with exactly one ``expected_uids`` page — the
    simplest case that still has a real planted token to lose."""
    pages = load_core_pages()
    probes = load_probes()
    page = next(p for p in pages if p.uid == "policy-pto")
    probe = next(p for p in probes if p.id == "pto_allowance")
    assert probe.expected_uids == (page.uid,)
    return page, probe.id, probe.answer_tokens


def _scripted_clients(page: Page) -> tuple[_ScriptedClient, _ScriptedClient, dict[str, Any]]:
    """Build the (classify_client, write_client) pair for one page's round
    trip, plus the shared mutable state that tells both which
    :class:`Observation` is currently being processed.

    The classify responder resolves every raw file straight to *page* — it
    is not exercising classification judgment (this test is not a
    ``LAYER_CLASSIFY`` eval), only the compile WIRING downstream of a
    classify decision that is already known to be correct. The write
    responder answers the two prompt shapes ``athenaeum.tiers`` actually
    sends (see ``CREATE_TEMPLATE``/``MERGE_TEMPLATE`` markers "## Entity to
    create" / "## New observation") by echoing the CURRENT observation's own
    text back verbatim — an anchored ``append_section`` op for a merge, a
    bare body for a create — so a planted token is never paraphrased away
    by this test's own fake, only carried through whatever the real
    ``apply_merge_ops``/frontmatter-render code does with it.
    """
    state: dict[str, Any] = {"observation": None, "created": False}

    def classify_responder(params: dict[str, Any]) -> str:
        if state["created"]:
            return "[]"
        state["created"] = True
        observation: Observation = state["observation"]
        return json.dumps(
            [
                {
                    "name": page.name,
                    "entity_type": page.type,
                    "tags": list(page.tags),
                    "access": "internal",
                    "observations": observation.body,
                }
            ]
        )

    def write_responder(params: dict[str, Any]) -> str:
        observation: Observation = state["observation"]
        user_msg = params["messages"][0]["content"]
        if "## Entity to create" in user_msg:
            return f"# {page.name}\n\n{observation.body}\n"
        if "## New observation" in user_msg:
            return json.dumps({"ops": [{"op": "append_section", "text": observation.body}]})
        raise AssertionError(f"unrecognized write-tier prompt: {user_msg[:200]!r}")

    classify_client = _ScriptedClient(classify_responder)
    write_client = _ScriptedClient(write_responder)
    return classify_client, write_client, state


def test_observations_carry_the_planted_answer_tokens() -> None:
    """Sanity check on the GENERATOR's own ground truth (independent of the
    librarian entirely): every one of the probe's planted tokens must be
    distributed onto at least one generated observation, or the round trip
    below would be checking nothing."""
    page, _probe_id, answer_tokens = _round_trip_page()
    observations = generate_page_observations(page, load_probes())
    assert len(observations) >= 2, "need more than one observation to exercise create+merge"
    carried = {t for obs in observations for t in obs.answer_tokens}
    assert set(answer_tokens) <= carried, (
        f"generator dropped planted token(s) before the librarian ever ran: "
        f"missing={set(answer_tokens) - carried}"
    )


def test_raw_observation_round_trip_survives_compile_wiring(tmp_path: Path) -> None:
    """page -> observations -> librarian (scripted client) -> page whose
    answer_tokens survive. See module docstring for exactly what this does
    and does not establish."""
    page, _probe_id, answer_tokens = _round_trip_page()
    probes = load_probes()
    observations = generate_page_observations(page, probes)

    stream_root = tmp_path / "knowledge"
    from tests.evals.corpus import ObservationStream

    ObservationStream(observations=observations).materialize(stream_root)
    raws = discover_raw_files(stream_root / "raw")
    assert len(raws) == len(observations), "materialized stream is not fully discoverable"

    by_body = {obs.body: obs for obs in observations}

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    classify_client, write_client, state = _scripted_clients(page)

    for raw in raws:
        observation = by_body.get(raw.content)
        assert observation is not None, (
            f"discovered raw file not traceable to an observation: {raw.path}"
        )
        state["observation"] = observation
        index = EntityIndex(wiki_root)
        process_one(
            raw,
            index,
            wiki_root,
            classify_client,
            valid_types=[page.type],
            valid_tags=list(page.tags),
            valid_access=VALID_ACCESS,
            usage=TokenUsage(),
            write_client=write_client,
        )

    compiled_pages = [p for p in wiki_root.glob("*.md") if not p.name.startswith("_")]
    compiled_names = [p.name for p in compiled_pages]
    assert len(compiled_pages) == 1, (
        f"expected exactly one compiled page for {page.uid!r}, got {compiled_names}"
    )
    meta, body = parse_frontmatter(compiled_pages[0].read_text(encoding="utf-8"))
    assert meta.get("name") == page.name

    missing = [t for t in answer_tokens if t not in body]
    assert not missing, (
        f"answer_tokens dropped during compile: {missing}. Compiled body:\n{body}"
    )
    # Every classify call after the first must have been asked to find NEW
    # entities in text that is entirely about the one page already created
    # -- the scripted responder always says "none", but the call itself
    # still exercises the real tier2_classify plumbing (prompt assembly,
    # response parsing) on every subsequent observation.
    assert len(classify_client.calls) == len(observations)


@pytest.mark.parametrize("drop_index", [0, 1, 2])
def test_dropping_an_observation_is_measurable_as_a_lost_token(
    tmp_path: Path, drop_index: int
) -> None:
    """Generator-ground-truth check (offline, no librarian): dropping the
    one observation that carries the probe's planted token from the stream
    removes that token from the reachable ground truth -- proving the
    per-observation ``answer_tokens`` attribution in
    :func:`tests.evals.corpus.generate_page_observations` is real ground
    truth a caller can act on, not decoration. This is the property AC1
    asks for ("a compile that drops an observation is measurable as a lost
    observation"), checked directly on the generator's output rather than by
    re-running the full librarian round trip for every drop."""
    page, _probe_id, answer_tokens = _round_trip_page()
    observations = generate_page_observations(page, load_probes())
    if drop_index >= len(observations):
        pytest.skip("page has fewer observations than this parametrized drop index")
    remaining = observations[:drop_index] + observations[drop_index + 1 :]
    dropped = observations[drop_index]

    remaining_tokens = {t for obs in remaining for t in obs.answer_tokens}
    if dropped.answer_tokens:
        for token in dropped.answer_tokens:
            assert token not in remaining_tokens, (
                f"dropping observation {dropped.uid!r} should have removed "
                f"token {token!r} from the reachable set"
            )
    else:
        assert set(answer_tokens) <= remaining_tokens
