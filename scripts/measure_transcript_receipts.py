"""Synthetic receipts-phase benchmark (athenaeum#2048).

Builds N synthetic memories + transcripts in a temp dir (never touches a live
store), runs the receipt writer for a cold night, an unchanged night and a
resumed night, and prints ledger opens / transcript reads / wall time.

Usage: python scripts/measure_transcript_receipts.py [N]
"""

import json
import sys
import tempfile
import time
from pathlib import Path

import athenaeum.transcript_receipts as tr
from athenaeum.models import AutoMemoryFile
from athenaeum.runlock import RunLock

N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
tmp = Path(tempfile.mkdtemp())
pr, wiki = tmp / "projects", tmp / "wiki"
mem = tmp / "mem"
mem.mkdir()


def rec(t):
    return {"type": "user", "message": {"role": "user", "content": t}}


files = []
for i in range(N):
    scope = f"scope{i % 10}"
    (pr / scope).mkdir(parents=True, exist_ok=True)
    lines = [rec(f"filler {j}") for j in range(150)] + [rec(f"durable fact number {i}")]
    (pr / scope / f"s{i}.jsonl").write_text("\n".join(json.dumps(row) for row in lines) + "\n")
    p = mem / f"m{i}.md"
    p.write_text(f"---\nname: m{i}\n---\ndurable fact number {i}\n")
    files.append(
        AutoMemoryFile(
            path=p,
            origin_scope=scope,
            memory_type="feedback",
            origin_session_id=f"s{i}",
            origin_turn=1,
        )
    )
counts = {"ledger": 0, "transcript": 0}
orig_rb, orig_rt = Path.read_bytes, Path.read_text


def classify(self):
    s = str(self)
    if "_transcript_receipts" in s:
        return "ledger"
    if s.startswith(str(pr)):
        return "transcript"


def rb(self, *a, **k):
    c = classify(self)
    if c:
        counts[c] += 1
    return orig_rb(self, *a, **k)


def rt(self, *a, **k):
    c = classify(self)
    if c:
        counts[c] += 1
    return orig_rt(self, *a, **k)


Path.read_bytes, Path.read_text = rb, rt
cfg = {"librarian": {"transcript_receipts_enabled": True}}
lock = RunLock(tmp)


def night(label):
    counts.update(ledger=0, transcript=0)
    t = time.perf_counter()
    with lock:
        n = tr.write_receipts_for_files(wiki, files, config=cfg, projects_root=pr, lock=lock)
    dt = time.perf_counter() - t
    print(
        f"{label}: N={N} processed={n} secs={dt:.2f} ledger_opens={counts['ledger']} "
        f"({counts['ledger'] / N:.2f}/mem) transcript_reads={counts['transcript']} "
        f"({counts['transcript'] / N:.2f}/mem) "
        f"ledger_lines={len(tr.iter_receipts(wiki))}"
    )


night("night1 cold")
night("night2 unchanged")
for i in range(N):  # resume: append unrelated turns
    with (pr / f"scope{i % 10}" / f"s{i}.jsonl").open("a") as fh:
        fh.write(json.dumps(rec("later turn")) + "\n")
night("night3 resumed")
