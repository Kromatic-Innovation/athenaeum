# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for search benchmarks.

Builds a deterministic synthetic wiki once per module so p95 numbers are
comparable across runs on the same machine. Every page has a predictable
frontmatter block plus a body seeded from a fixed vocabulary — no
dependency on the real ``wiki_dir`` fixture in ``tests/conftest.py``
(which is shaped for correctness tests, not throughput).
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

# Fixed vocabulary — small enough that queries land, big enough that
# scoring has to actually do work on every page.
_VOCAB = (
    "athenaeum librarian recall memory wiki entity fintech startup series "
    "lean customer discovery validation pivot runway burn traction cohort "
    "revenue margin churn retention funnel acquisition activation referral "
    "onboarding engagement conversion experiment hypothesis assumption risk "
    "metric benchmark latency throughput reliability observability telemetry "
    "deployment staging production rollback canary blast radius postmortem "
    "architecture database index vector keyword fulltext search ranking score "
    "embedding cluster similarity distance cosine nearest neighbor retrieval "
).split()

# Page count — big enough that keyword backend's scan-on-query dominates
# query time (so we're measuring the search path, not fixture overhead)
# but small enough that a cold build stays well under a second.
_PAGE_COUNT = 200


def _seeded_page(rng: random.Random, idx: int) -> tuple[str, str]:
    """Return ``(filename, body)`` for one synthetic wiki page."""
    name_words = rng.sample(_VOCAB, 3)
    tags = rng.sample(_VOCAB, 4)
    body_words = [rng.choice(_VOCAB) for _ in range(120)]
    slug = f"bench_page_{idx:04d}_{'_'.join(name_words)}"
    filename = f"{slug}.md"
    body = (
        "---\n"
        f"name: {' '.join(name_words).title()}\n"
        f"description: Synthetic bench page {idx}.\n"
        f"tags: [{', '.join(tags)}]\n"
        "type: bench\n"
        "---\n\n"
        + " ".join(body_words)
        + "\n"
    )
    return filename, body


@pytest.fixture(scope="module")
def bench_wiki(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Deterministic synthetic wiki with ``_PAGE_COUNT`` pages."""
    wiki = tmp_path_factory.mktemp("bench_wiki")
    rng = random.Random(0xA7E2)  # fixed seed → reproducible across runs
    for idx in range(_PAGE_COUNT):
        fname, body = _seeded_page(rng, idx)
        (wiki / fname).write_text(body, encoding="utf-8")
    return wiki


@pytest.fixture(scope="module")
def bench_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Cache directory shared across backends within a module run."""
    return tmp_path_factory.mktemp("bench_cache")


# Queries chosen to land in the bench vocabulary so every backend returns
# hits (zero-hit queries short-circuit on some backends and skew timings).
BENCH_QUERIES: tuple[str, ...] = (
    "customer discovery validation",
    "latency throughput benchmark",
    "embedding cluster similarity",
    "revenue margin churn retention",
    "deployment staging rollback",
)
