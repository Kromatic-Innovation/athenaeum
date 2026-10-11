#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Live smoke test for :class:`athenaeum.decision_provider.JevDecisionBackend`
(issue athenaeum#2054; ``--choice`` added for athenaeum#2060).

Makes exactly ONE call against the live Jev (TypeSafe AI) endpoint on a
synthetic, non-sensitive string, and prints the probability (or choice),
latency and usage. No PII, no corpus data -- the question is fabricated by
this script. Default is a Noul call; ``--choice`` runs a two-option Choice
question instead, to exercise the `{label: description}` criteria mapping
(athenaeum#2060) against the live endpoint.

**Refuses to run without ``JEV_API_KEY``.** Never logs or prints the key
itself; presence is checked, the value is never read back out. Not run in
CI -- this makes a real, billed network call.

Usage::

    set -a; . ~/.cache/athenaeum/config.env; set +a
    python3 scripts/jev_smoke.py
    python3 scripts/jev_smoke.py --choice
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from athenaeum.decision_provider import JevDecisionBackend


def _run_noul(backend: JevDecisionBackend) -> int:
    start = time.monotonic()
    result = backend.decide(
        question_id="smoke:noul",
        kind="noul",
        instructions="Answer yes or no: is the sky typically blue on a clear day?",
        state="This is a synthetic smoke-test string with no real-world data.",
    )
    latency_ms = (time.monotonic() - start) * 1000

    print(f"probability: {result.probability}")
    print(f"latency_ms: {latency_ms:.1f}")
    print(f"input_tokens: {result.input_tokens}")
    print(f"output_tokens: {result.output_tokens}")
    return 0


def _run_choice(backend: JevDecisionBackend) -> int:
    start = time.monotonic()
    result = backend.decide(
        question_id="smoke:choice",
        kind="choice",
        instructions="Which color is the sky typically on a clear day?",
        state="This is a synthetic smoke-test string with no real-world data.",
        criteria={"blue": "the color blue", "green": "the color green"},
    )
    latency_ms = (time.monotonic() - start) * 1000

    print(f"choice: {result.choice}")
    print(f"probabilities: {result.probabilities}")
    print(f"confidence: {result.confidence}")
    print(f"latency_ms: {latency_ms:.1f}")
    print(f"input_tokens: {result.input_tokens}")
    print(f"output_tokens: {result.output_tokens}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--choice",
        action="store_true",
        help="Run a two-option Choice question instead of the default Noul call.",
    )
    args = parser.parse_args()

    if not os.environ.get("JEV_API_KEY"):
        print(
            "jev_smoke: refusing to run -- JEV_API_KEY is not set. "
            "This script makes a real, billed call and will not fall back "
            "to a fake transport.",
            file=sys.stderr,
        )
        return 1

    backend = JevDecisionBackend()
    if args.choice:
        return _run_choice(backend)
    return _run_noul(backend)


if __name__ == "__main__":
    raise SystemExit(main())
