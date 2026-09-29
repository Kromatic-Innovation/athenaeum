# SPDX-License-Identifier: Apache-2.0
"""Both ``UserPromptSubmit`` hooks render the same bytes (issue athenaeum#1912).

``tests/evals/test_adapter_overflow_breadcrumb_1905.py`` pins one thing about
the packaged adapter: that it SAYS candidates were withheld wherever the shell
hook does. It deliberately stops short of comparing the two notices' numbers,
and it compares only a six-probe prefix. That restraint was right at the time
— athenaeum#1905 closed the presence/absence gap and nothing else — but it left
the larger regression uncovered: after that fix the shipped
``push_breadcrumb_pull`` arm was still 5.0 points below the 2026-09-19
shell-hook reading, and a shared-index re-derivation attributed the residual to
the two hooks building DIFFERENT QUERY TERMS for the same prompt.

This module is the end-to-end pin for the fix: the adapter's
``additionalContext`` must equal the shell hook's **byte for byte**, bullet
list and overflow notice included, over every ``build_corpus("core")`` probe,
on ONE shared hook index.

Three properties, each load-bearing:

* **One shared index.** Two separately built chromadb collections are not
  guaranteed identical, so comparing across two index builds would fold
  build noise into every difference. ``tests/evals/hook_divergence.py``'s
  ``--mode adapter-vs-adapter`` measures that floor directly; this test
  removes it by construction.
* **A fresh session id per (probe, side) call.** Sharing an index never
  implies sharing a seen-file — both hooks exclude pages already pushed in
  the same session, and a reused id would starve one side's later probes.
* **Normalization limited to two known shell-hook defects.** The trailing
  newline and the spurious empty bullet, both named in
  ``test_adapter_overflow_breadcrumb_1905.py``'s module docstring and
  implemented once in :func:`tests.evals.hook_divergence.normalize`. Nothing
  else is normalized away, so this really is a byte comparison of everything
  that reaches the model.

**Cost and skipping.** No model call, no ``claude`` binary, no API key — the
harness env pins BOTH hooks to their offline regex term extractor. It spawns
two subprocesses per probe against one prebuilt index; the full 48-probe
sweep runs in under a minute. It carries no ``eval``/``embedding``/``rollout``
marker, so it is part of the DEFAULT pytest selection, and it SKIPS (never
passes) when ``bash``, ``jq``, an FTS5-capable ``sqlite3``, the packaged
console script, ``chromadb`` or a built vector index is unavailable — a
silent pass on a runner that could not run either hook would be worse than no
test at all.
"""

from __future__ import annotations

import pytest

from tests.evals.hook_divergence import (
    ClassifierUnavailable,
    ProbeResult,
    missing_prerequisites,
    normalize,
    run,
    split_render,
)
from tests.evals.rollout import resolve_user_prompt_hook

#: Probes whose two renders are still expected to differ after the term fix,
#: mapped to the MECHANISM that explains each one. Empty as of the fix.
#:
#: This is an enumeration, never a tolerance: :func:`test_no_unexplained_divergence`
#: fails on any probe outside it, and :func:`test_every_enumerated_divergence_still_diverges`
#: fails on any probe INSIDE it that has since been fixed — so a stale entry
#: cannot quietly widen the exemption. Adding an entry requires stating the
#: mechanism here; "flaky" is not a mechanism.
KNOWN_DIVERGENCES: dict[str, str] = {}


@pytest.fixture(scope="module")
def comparison() -> list[ProbeResult]:
    missing = missing_prerequisites(backend="vector")
    if missing:
        pytest.skip("hook byte-equivalence needs: " + ", ".join(missing))
    if resolve_user_prompt_hook().suffix == ".sh":
        pytest.skip(
            "the default UserPromptSubmit hook resolves to a shell script, so this "
            "module would compare the shell hook with itself and prove nothing"
        )
    try:
        return run(mode="adapter-vs-shell", backend="vector", scale="core")
    except ClassifierUnavailable as exc:  # pragma: no cover — runner-dependent
        pytest.skip(str(exc))


def test_the_sweep_covers_every_core_probe(comparison: list[ProbeResult]) -> None:
    """Vacuity guard: a comparison that silently swept a prefix (or nothing)
    would let every assertion below pass on an empty set."""
    from tests.evals.corpus import build_corpus

    assert {r.probe_id for r in comparison} == {p.id for p in build_corpus("core").probes}
    assert len(comparison) == 48


def test_both_hooks_rendered_something(comparison: list[ProbeResult]) -> None:
    """Both sides must have produced real context on most probes — two hooks
    that both returned the empty string would be byte-equal and meaningless."""
    rendered = [r for r in comparison if r.left.strip() and r.right.strip()]
    assert len(rendered) >= 40, (
        f"only {len(rendered)}/48 probes produced context on BOTH hooks; the index "
        "build or the query path is broken, not the renders"
    )


def test_at_least_one_probe_carries_an_overflow_notice(comparison: list[ProbeResult]) -> None:
    """Vacuity guard for the notice half of the byte comparison — which is the
    half ``test_adapter_overflow_breadcrumb_1905.py`` deliberately left
    count-agnostic and delegates here."""
    with_notice = [r for r in comparison if split_render(normalize(r.left))[1]]
    assert with_notice, (
        "no core probe withheld any candidate, so this module proved nothing about "
        "the overflow notice's TEXT"
    )


def test_no_unexplained_divergence(comparison: list[ProbeResult]) -> None:
    """The issue's byte-equivalence criterion, with no blanket tolerance.

    Reported as one failure listing every offending probe and the first
    differing line on each side, because a term-rule regression moves many
    probes at once and a per-probe assertion would name only the first.
    """
    offenders: dict[str, tuple[str, str]] = {}
    for result in comparison:
        if result.probe_id in KNOWN_DIVERGENCES:
            continue
        left, right = normalize(result.left), normalize(result.right)
        if left != right:
            offenders[result.probe_id] = _first_difference(left, right)
    assert not offenders, (
        "the adapter and the shell hook render different bytes for these probes "
        f"(neither fixed nor enumerated in KNOWN_DIVERGENCES): {offenders}"
    )


def test_every_enumerated_divergence_still_diverges(comparison: list[ProbeResult]) -> None:
    """A stale exemption is a silent hole. Any probe listed in
    :data:`KNOWN_DIVERGENCES` that now matches must be deleted from it."""
    by_id = {r.probe_id: r for r in comparison}
    stale = [
        probe_id
        for probe_id in KNOWN_DIVERGENCES
        if probe_id in by_id and normalize(by_id[probe_id].left) == normalize(by_id[probe_id].right)
    ]
    assert not stale, (
        f"these probes no longer diverge and must be removed from KNOWN_DIVERGENCES: {stale}"
    )


def test_every_enumerated_divergence_names_a_mechanism() -> None:
    """``KNOWN_DIVERGENCES`` is an enumeration of explained differences. An
    entry with an empty or hand-waving value is a tolerance wearing a
    dictionary's clothes."""
    for probe_id, mechanism in KNOWN_DIVERGENCES.items():
        assert len(mechanism.strip()) >= 30, (
            f"{probe_id}: state the mechanism, not a placeholder ({mechanism!r})"
        )


def test_normalization_removes_exactly_the_two_shell_hook_defects() -> None:
    """The normalizer is the only thing standing between "byte-equal" and
    "equal enough", so it gets its own pin.

    In process, on literals — no hook, no index, no corpus.
    """
    preamble = "[Knowledge context] relevant pages:"
    bullet = "  - alpha — a page"
    shell_shaped = f"{preamble}\n{bullet}\n  - \nmemory has 2 more matching results\n"
    adapter_shaped = f"{preamble}\n{bullet}\nmemory has 2 more matching results"

    assert normalize(shell_shaped) == normalize(adapter_shaped) == adapter_shaped

    # ...and nothing beyond those two. Ordering, spacing inside a bullet and
    # notice wording all survive normalization.
    assert normalize("  - beta\n  - alpha") != normalize("  - alpha\n  - beta")
    assert normalize("  - alpha  — a page") != normalize("  - alpha — a page")
    assert normalize("memory has 2 more") != normalize("memory has 3 more")
    # An interior blank line is not one of the two defects and is preserved.
    assert normalize(f"{bullet}\n\n{bullet}") == f"{bullet}\n\n{bullet}"


def _first_difference(left: str, right: str) -> tuple[str, str]:
    left_lines, right_lines = left.split("\n"), right.split("\n")
    for index in range(max(len(left_lines), len(right_lines))):
        left_line = left_lines[index] if index < len(left_lines) else "<absent>"
        right_line = right_lines[index] if index < len(right_lines) else "<absent>"
        if left_line != right_line:
            return (f"adapter[{index}]={left_line!r}", f"shell[{index}]={right_line!r}")
    return ("", "")  # pragma: no cover — only reached if the strings are equal
