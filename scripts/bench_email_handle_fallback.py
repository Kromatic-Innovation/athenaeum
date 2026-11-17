#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Before/after latency for email-shaped recall lookups (athenaeum#2714).

Generates a SYNTHETIC corpus (example.org / example.com addresses, invented
names) in a scratch dir, runs the registry build (``athenaeum registry``),
then times ``resolve_handle_query`` for five address shapes: contact-store
hit, registry-handle-only hit, body-only hit, ambiguous body match, no-match.
Run it from a checkout of the code under test; run it on ``develop`` for the
"before" numbers (there the body-only/ambiguous shapes report ``no-match``).

Usage: ``python scripts/bench_email_handle_fallback.py [--pages N] [--queries N]``
Prints a JSON report; writes nothing outside its own scratch directory.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from athenaeum import pii
from athenaeum._cmd_index import cmd_registry
from athenaeum.identity_resolution import resolve_handle_query

CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}


def build_corpus(knowledge: Path, n: int) -> None:
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    contacts = pii.contacts_surface_root(knowledge, CONFIG)
    contacts.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        uid = f"{i:08x}"
        extra = ""
        body = f"Person {i} works on widget calibration."
        if i % 3 == 0:  # contact-store address
            (contacts / f"{uid}-c.md").write_text(
                f"---\nuid: {uid}\npii: true\nemails:\n  - user{i}@example.org\n---\n\nx\n"
            )
        elif i % 3 == 1:  # registry handle only
            extra = f"alt_emails:\n  - alias{i}@example.org\n"
        else:  # body only
            body += f" Contact: body{i}@example.com or shared@example.com."
        (wiki / f"{uid}-person-{i}.md").write_text(
            f"---\nuid: {uid}\nname: Person {i}\ntype: person\n{extra}---\n\n{body}\n"
        )


def time_queries(knowledge: Path, cache: Path, addr: str, q: int) -> dict[str, object]:
    ms: list[float] = []
    reason = None
    for _ in range(q):
        t = time.perf_counter()
        res = resolve_handle_query(
            knowledge, knowledge / "wiki", addr, config=CONFIG, cache_dir=cache
        )
        ms.append((time.perf_counter() - t) * 1000)
        assert res is not None
        reason = "resolved" if res.resolved else res.reason
    return {"outcome": reason, "median_ms": round(statistics.median(ms), 2),
            "mean_ms": round(statistics.mean(ms), 2), "max_ms": round(max(ms), 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=15000)
    ap.add_argument("--queries", type=int, default=50)
    a = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="bench2714-"))
    try:
        knowledge, cache = root / "k", root / "cache"
        build_corpus(knowledge, a.pages)
        t = time.perf_counter()
        cmd_registry(argparse.Namespace(path=knowledge, out=None, stdout=False))
        build_s = time.perf_counter() - t
        shapes = {
            "contact_store_hit": "user3@example.org",
            "registry_handle_only": "alias4@example.org",
            "body_only": "body5@example.com",
            "ambiguous_body": "shared@example.com",
            "no_match": "ghost@example.org",
        }
        # one untimed warm-up per shape (builds the persisted contact/entity caches)
        for addr in shapes.values():
            time_queries(knowledge, cache, addr, 1)
        out = {n: time_queries(knowledge, cache, ad, a.queries) for n, ad in shapes.items()}
        print(json.dumps({"pages": a.pages, "queries_per_shape": a.queries,
                          "registry_build_s": round(build_s, 2), "shapes": out}, indent=2))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
