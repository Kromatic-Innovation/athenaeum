#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Before/after timing for the persisted contact-index cache (issue athenaeum#2182).

Generates a SYNTHETIC fixture corpus of ~13k contact records under a scratch
directory (never under this repo, never `/knowledge`, never any live store —
only `tempfile.mkdtemp()`), then times three lookup shapes against it:

- **single-page**: an address that resolves to exactly one record.
- **ambiguous**: an address shared by several records.
- **no-match**: an address no record lists at all.

"Before" times a fresh `pii.ExcludedRecordIndex` built from scratch for every
single lookup — the pre-athenaeum#2182 per-call cost this issue's motivation
describes ("about 0.8s of every handle-shaped recall is spent rebuilding
pii.ExcludedRecordIndex"). "After" times the WARM persisted cache from
`athenaeum.contact_index_cache` — the cold build (which also costs a full
scan) is reported separately and excluded from the warm average, since a
cold cache is paid at most once per corpus per cache dir, not once per call.

Usage: ``python scripts/bench_contact_index_cache.py [--records N] [--repeat N]``

Prints a JSON report to stdout; writes nothing outside its own scratch
directory, which is removed on exit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from athenaeum import contact_index_cache as cic  # noqa: E402
from athenaeum import pii  # noqa: E402

SINGLE_ADDRESS = "user00000@example.org"
AMBIGUOUS_ADDRESS = "shared@example.org"
NO_MATCH_ADDRESS = "ghost-never-written@example.org"


def _write_record(contacts_root: Path, filename: str, uid: str, emails: list[str]) -> None:
    body = "\n".join(f"  - {e}" for e in emails)
    (contacts_root / filename).write_text(
        f"---\nuid: {uid}\npii: true\nemails:\n{body}\n---\n\nArchival data.\n",
        encoding="utf-8",
    )


def build_fixture_corpus(contacts_root: Path, n_records: int) -> None:
    """Write *n_records* synthetic contact records plus the three probe addresses."""
    contacts_root.mkdir(parents=True, exist_ok=True)
    for i in range(n_records):
        uid = f"b{i:07x}"[:8]
        _write_record(contacts_root, f"contact-{i:06d}.md", uid, [f"user{i:05d}@example.org"])
    # Two records sharing one address -- the "ambiguous" case.
    _write_record(contacts_root, "contact-amb-a.md", "ambigua1", [AMBIGUOUS_ADDRESS])
    _write_record(contacts_root, "contact-amb-b.md", "ambigub2", [AMBIGUOUS_ADDRESS])
    # NO_MATCH_ADDRESS and SINGLE_ADDRESS are deliberately never/already written
    # (SINGLE_ADDRESS is user00000@example.org from the loop above).


def _time_uncached_lookup(contacts_root: Path, address: str) -> float:
    start = time.perf_counter()
    index = pii.ExcludedRecordIndex(contacts_root)
    index.all_by_identifier(address)
    return time.perf_counter() - start


def _time_cached_lookup(contacts_root: Path, cache_dir: Path, address: str) -> float:
    start = time.perf_counter()
    index = cic.load_contact_index(contacts_root, cache_dir)
    index.all_by_identifier(address)
    return time.perf_counter() - start


def run(n_records: int, repeat: int) -> dict[str, object]:
    scratch = Path(tempfile.mkdtemp(prefix="athenaeum-contact-index-bench-"))
    try:
        contacts_root = scratch / "contacts"
        cache_dir = scratch / "cache"
        build_fixture_corpus(contacts_root, n_records)

        addresses = {
            "single_page": SINGLE_ADDRESS,
            "ambiguous": AMBIGUOUS_ADDRESS,
            "no_match": NO_MATCH_ADDRESS,
        }

        before: dict[str, list[float]] = {k: [] for k in addresses}
        for _ in range(repeat):
            for name, address in addresses.items():
                before[name].append(_time_uncached_lookup(contacts_root, address))

        cold_start = time.perf_counter()
        cic.load_contact_index(contacts_root, cache_dir)
        cold_build_s = time.perf_counter() - cold_start

        after: dict[str, list[float]] = {k: [] for k in addresses}
        for _ in range(repeat):
            for name, address in addresses.items():
                after[name].append(_time_cached_lookup(contacts_root, cache_dir, address))

        def _avg(values: list[float]) -> float:
            return sum(values) / len(values)

        return {
            "n_records": n_records,
            "repeat": repeat,
            "cold_cache_build_s": cold_build_s,
            "before_s": {k: _avg(v) for k, v in before.items()},
            "after_warm_s": {k: _avg(v) for k, v in after.items()},
            "speedup_x": {
                k: (_avg(before[k]) / _avg(after[k])) if _avg(after[k]) > 0 else float("inf")
                for k in addresses
            },
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=13_000)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(run(args.records, args.repeat), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
