# SPDX-License-Identifier: Apache-2.0
"""Determinism + ground-truth coverage for the raw-observation generator
(issue athenaeum#1726 AC1).

Complements ``tests/evals/test_raw_observation_roundtrip.py`` (which proves
the generator's output survives the librarian's compile wiring) and
``tests/test_eval_corpus_leakage.py`` (which proves it carries no contact
data) with the properties that belong to the generator alone, offline and
without a wiki, a probe grade, or a subprocess anywhere in the loop.
"""

from __future__ import annotations

import pytest

from tests.evals.corpus import (
    GENERATOR_VERSION,
    Observation,
    generate_core_observations,
    generate_page_observations,
    load_core_pages,
    load_probes,
)

_ObsFields = tuple[str, str, str, str, str, "tuple[str, ...]"]


def _fields(observations: list[Observation]) -> list[_ObsFields]:
    return [
        (o.uid, o.page_uid, o.source, o.timestamp, o.uuid8, o.answer_tokens) for o in observations
    ]


def test_generate_page_observations_is_deterministic_for_same_seed() -> None:
    pages = load_core_pages()
    probes = load_probes()
    page = next(p for p in pages if p.uid == "policy-pto")

    first = generate_page_observations(page, probes, seed=42)
    second = generate_page_observations(page, probes, seed=42)

    assert _fields(first) == _fields(second)
    assert [o.body for o in first] == [o.body for o in second]


def test_generate_core_observations_is_deterministic_for_same_seed() -> None:
    first = generate_core_observations(seed=20260908)
    second = generate_core_observations(seed=20260908)

    assert _fields(first.observations) == _fields(second.observations)
    assert [o.body for o in first.observations] == [o.body for o in second.observations]
    assert first.answer_tokens() == second.answer_tokens()


def test_generate_page_observations_is_invariant_across_scale() -> None:
    """AC1 contract: deterministic for ``(GENERATOR_VERSION, seed, scale)``.
    This generator satisfies that STRONGER than required -- it is invariant
    across scale entirely, because only the hand-authored ``core`` tier
    (which every scale shares unchanged, per ``build_corpus``) carries the
    ground truth a write-path measurement needs. Pinned here as a contract,
    not left as an inference from ``scale`` simply being unused."""
    pages = load_core_pages()
    probes = load_probes()
    page = next(p for p in pages if p.uid == "policy-pto")

    core_scale = generate_page_observations(page, probes, seed=42, scale="core")
    large_scale = generate_page_observations(page, probes, seed=42, scale="large")

    assert _fields(core_scale) == _fields(large_scale)
    assert [o.body for o in core_scale] == [o.body for o in large_scale]


def test_generate_page_observations_rejects_an_unknown_scale() -> None:
    pages = load_core_pages()
    probes = load_probes()
    page = next(p for p in pages if p.uid == "policy-pto")

    with pytest.raises(ValueError, match="unknown scale"):
        generate_page_observations(page, probes, scale="not-a-real-scale")


def test_generate_core_observations_is_invariant_across_scale() -> None:
    """Same contract as above, at the whole-stream grain: the SAME seed at
    two different scales returns identical observations, order included --
    the interleave shuffle is keyed on ``(GENERATOR_VERSION, seed)`` only,
    never on ``scale``."""
    core_scale = generate_core_observations(seed=7, scale="core")
    large_scale = generate_core_observations(seed=7, scale="large")

    assert _fields(core_scale.observations) == _fields(large_scale.observations)
    assert [o.body for o in core_scale.observations] == [o.body for o in large_scale.observations]
    assert core_scale.answer_tokens() == large_scale.answer_tokens()
    assert core_scale.scale == "core"
    assert large_scale.scale == "large"


def test_generate_core_observations_rejects_an_unknown_scale() -> None:
    with pytest.raises(ValueError, match="unknown scale"):
        generate_core_observations(seed=1, scale="not-a-real-scale")


def test_generate_core_observations_interleave_order_varies_by_seed() -> None:
    """Not a hard requirement of AC1 (which asks only for reproducibility
    per seed), but pins that ``seed`` is load-bearing on this generator's
    output, not a decorative parameter two different values happen to
    collapse to the same order under."""
    first = generate_core_observations(seed=1)
    second = generate_core_observations(seed=2)

    assert [o.uid for o in first.observations] != [o.uid for o in second.observations]
    # The SET of observations is identical -- only the order differs.
    assert {o.uid for o in first.observations} == {o.uid for o in second.observations}


def test_uuid8_is_process_stable_not_builtin_hash() -> None:
    """Regenerating in a FRESH call (a fresh ``random.Random``, a fresh
    string) must not depend on ``PYTHONHASHSEED`` -- the same discipline
    ``tests.evals.corpus._stable_hash`` enforces for the page generator
    (module docstring: "process-stable... anything derived from builtin
    hash() varies between runs")."""
    pages = load_core_pages()
    probes = load_probes()
    page = next(p for p in pages if p.uid == "policy-pto")

    observations = generate_page_observations(page, probes, seed=7)
    for obs in observations:
        assert len(obs.uuid8) == 8
        int(obs.uuid8, 16)  # hex digest, not a str(hash())-derived value


def test_every_planted_probe_token_is_reachable_from_core_observations() -> None:
    """Ground truth check: every ``answer_tokens`` value any non-abstention
    probe plants on a core page must be carried by at least one generated
    observation -- otherwise the AC2 round trip (or a live Phase 2 run)
    would be unable to measure that token's survival at all, independent of
    anything the librarian or a model does."""
    probes = load_probes()
    stream = generate_core_observations()
    carried = stream.answer_tokens()

    all_probe_tokens = {token for probe in probes for token in probe.answer_tokens}
    missing = all_probe_tokens - carried
    assert not missing, f"planted token(s) unreachable from any observation: {missing}"


def test_generator_version_is_the_page_generators_shared_constant() -> None:
    """The observation generator does not mint its own version constant --
    it inverts ``GENERATOR_VERSION``-versioned core page bytes, so a bump to
    that constant (a change to what a page RENDERS as) is the same
    reproducibility boundary for observations as it already is for
    :func:`tests.evals.corpus.build_corpus`."""
    assert isinstance(GENERATOR_VERSION, int)


# ---------------------------------------------------------------------------
# Transient (``retain=False``) ground truth and production intake shape --
# issue athenaeum#1824.
# ---------------------------------------------------------------------------


def test_the_stream_carries_transient_observations_of_all_three_kinds() -> None:
    """Without ``retain=False`` ground truth the write-path measurement
    rewards remembering everything, so a store that hoarded an afternoon's
    outage note would score identically to one that correctly dropped it."""
    stream = generate_core_observations()
    transient = [obs for obs in stream.observations if not obs.retain]

    assert len(transient) >= 3
    assert all(obs.answer_tokens for obs in transient), (
        "a transient observation with no planted token is unmeasurable"
    )
    assert all(obs.page_uid.startswith("transient-") for obs in transient), (
        "a transient observation names no corpus page"
    )


def test_transient_tokens_are_disjoint_from_the_retained_answer_tokens() -> None:
    """The two denominators must not overlap: a token that is both
    should-keep and should-drop makes every reading of the table ambiguous."""
    stream = generate_core_observations()

    assert stream.transient_tokens()
    assert not (stream.transient_tokens() & stream.answer_tokens())


def test_transient_tokens_occur_nowhere_in_the_compiled_corpus() -> None:
    """Same collision discipline every other planted token is held to: a
    transient token found in a store must be evidence of over-retention,
    never a coincidental word match against corpus prose."""
    from tests.evals.corpus import build_corpus

    stream = generate_core_observations()
    corpus_text = "\n".join(f"{page.name}\n{page.body}" for page in build_corpus("medium").pages)

    for token in stream.transient_tokens():
        assert token.lower() not in corpus_text.lower(), (
            f"transient token {token!r} collides with corpus text"
        )


def test_session_bundles_group_consecutive_observations_without_losing_any() -> None:
    """Production intake shape (issue athenaeum#1824): one raw file per
    SESSION, several observations each -- and the count of bundles is the
    number the librarian's file-counted ``max_files`` window sees."""
    stream = generate_core_observations()
    total = len(stream.observations)

    singletons = stream.session_bundles(session_size=1)
    assert len(singletons) == total

    bundled = stream.session_bundles(session_size=6)
    assert len(bundled) == -(-total // 6), "bundles must tile the stream exactly"
    assert [obs for _, group in bundled for obs in group] == stream.observations, (
        "bundling must preserve stream order and drop nothing"
    )
    assert all(len(group) <= 6 for _, group in bundled)
