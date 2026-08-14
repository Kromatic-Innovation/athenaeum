#!/usr/bin/env python3
"""Restore prose destroyed by the 2026-07-29 migrate-pii run (athenaeum#691).

For every `[contact redacted -> excluded surface]` marker in the live wiki, recover
the token it replaced from the pre-migration git revision and put it back IF that
token was never PII (a date, an id fragment, an issue-number list, an ISBN, a file
path, a host alias, a calendar id, a service account, a role/test address).

Genuine person contact data is LEFT MIGRATED. The email axis was 99.6% correct and
over-restoring it would be a PII regression.

Alignment is per-marker, not per-page: difflib gives the old-side span for each
replaced region, so every other byte of the current page -- including edits the
librarian made after the migration -- is preserved untouched.

Dry-run by default; --apply writes.

Provenance and patch history
----------------------------
Relocated into this repo 2026-08-14 (athenaeum#844). It previously lived, untracked,
at ``~/Desktop/athenaeum-691-restore_pii.py`` and was hand-patched twice with no
changelog and no review:

* 2026-08-02 -- diff-alignment fix. Alignment became per-marker rather than
  per-page, so difflib's old-side span is recovered for each replaced region and
  every other byte of the current page (including post-migration librarian edits)
  is preserved untouched.
* 2026-08-08 -- unbounded-file-size fix. ``difflib.SequenceMatcher(autojunk=False)``
  is a character-level diff and hung for hours on multi-MB text
  (``_pending_merges_archive.md`` is 22MB). Files above ``SIZE_LIMIT`` are now
  skipped and reported rather than scanned. Cost 3+ hours of wall-clock before
  being caught.

Both fixes are described in athenaeum#691's comments (2026-08-02T17:37Z and
2026-08-08T23:03Z).

One behavioural change was made during relocation, and only one: the
operator-specific half of ``SAFE_EMAIL_EXACT`` moved out of the source and into
live config (see :func:`safe_email_exact`). The classification LOGIC is unchanged.
This repo is public, and the previous hardcoded set contained a real address --
shipping the script that keeps contact data out of a published corpus must not
itself publish a contact address.
"""
from __future__ import annotations

import argparse
import collections
import difflib
import re
import subprocess
import sys
from pathlib import Path

KNOWLEDGE = Path.home() / "knowledge"
WIKI = KNOWLEDGE / "wiki"
MARKER = "[contact redacted → excluded surface]"

# The two bulk-migration commits; their parents hold the clean text.
MIGRATION_COMMITS = ["9e4381298", "f8e8fe0d9"]


def git_show(rev: str, relpath: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(KNOWLEDGE), "show", f"{rev}:{relpath}"],
        capture_output=True, text=True,
    )
    return r.stdout if r.returncode == 0 else None


# --- classification: is this token safe to put back? ------------------------

ISO_DATE = re.compile(r"^[(\[]?\d{4}-\d{2}-\d{2}\)?$")
YEAR_RANGE = re.compile(r"^[(\[]?(?:19|20)\d{2}-(?:19|20)\d{2}\)?$")
DATE_ISH = re.compile(r"(?:19|20)\d{2}-\d{2}(?:-\d{2})?")
ID_FRAGMENT = re.compile(r"^[(\[]?\d{6,9}\)?$")
NUM_LIST = re.compile(r"^[\d\s().-]*\d[-\d\s().]*$")
ISBN = re.compile(r"^\d{13}$")
DECIMAL = re.compile(r"^\d*\.\d+$")

# Generic, non-identifying entries safe to ship in a public repo. Anything
# operator-specific (a personal service address, a private host alias) belongs in
# live config, NOT here -- see safe_email_exact() below.
SAFE_EMAIL_EXACT_DEFAULT = frozenset(
    {
        "git@github.com",
        "root@example.com",
    }
)


def safe_email_exact(knowledge_root: Path | None = None) -> frozenset[str]:
    """Resolve the exact-match safe-email allowlist: defaults + live config.

    Operator-specific addresses are read from ``athenaeum.yaml``::

        pii:
          restore:
            safe_email_exact:
              - googledrive-tk@example.com

    Entries are case-folded to match ``classify``'s lowercased comparison. A
    missing or malformed block yields just the built-in defaults -- failing
    CLOSED, because an address absent from the allowlist stays REDACTED, which
    is the safe direction (this codebase's governing rule is that over-restoring
    is worse than under-restoring).
    """
    values: set[str] = set(SAFE_EMAIL_EXACT_DEFAULT)
    try:
        from athenaeum.config import load_config

        config = load_config(knowledge_root)
    except Exception:  # noqa: BLE001 - config is advisory; defaults still apply
        return frozenset(values)
    block = config.get("pii") if isinstance(config, dict) else None
    restore = block.get("restore") if isinstance(block, dict) else None
    configured = restore.get("safe_email_exact") if isinstance(restore, dict) else None
    if isinstance(configured, (list, tuple, set)):
        values.update(str(v).strip().lower() for v in configured if str(v).strip())
    return frozenset(values)


SAFE_EMAIL_EXACT = safe_email_exact()
SAFE_EMAIL_SUBSTR = (
    "group.calendar.google.com",
    "iam.gserviceaccount.com",
    "x-access-token",
)
SAFE_EMAIL_PREFIX = (
    "noreply@", "no-reply@", "donotreply@", "admin@", "support@", "info@",
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def classify(tok: str) -> str | None:
    """Return a restore-class name, or None to leave the redaction in place."""
    t = tok.strip()
    if not t:
        return None
    low = t.lower()

    if EMAIL_RE.search(t):
        addr_m = EMAIL_RE.search(t)
        addr = addr_m.group(0).lower() if addr_m else low
        if addr in SAFE_EMAIL_EXACT or low in SAFE_EMAIL_EXACT:
            return "email:host-alias/path"
        if any(s in low for s in SAFE_EMAIL_SUBSTR):
            return "email:service-id"
        if any(addr.startswith(p) for p in SAFE_EMAIL_PREFIX):
            return "email:role"
        if "test" in addr.split("@")[0]:
            return "email:test-account"
        return None  # a real person address -- stays migrated

    if ISO_DATE.match(t):
        return "date:iso"
    if YEAR_RANGE.match(t):
        return "date:year-range"
    if ISBN.match(t.replace("-", "")):
        return "isbn"
    if DECIMAL.match(t):
        return "decimal"
    if ID_FRAGMENT.match(t):
        return "id-fragment"
    if DATE_ISH.search(t) and not re.search(r"[A-Za-z]{3,}", t):
        return "date:embedded"
    if "--" in t and NUM_LIST.match(t):
        return "number-list"
    if NUM_LIST.match(t):
        digits = re.sub(r"\D", "", t)
        # A separator-bearing 10-15 digit run is a plausible real phone: keep redacted.
        if 10 <= len(digits) <= 15 and re.search(r"[-.\s()]", t):
            return None
        return "number-other"
    return None


def restore_page(relpath: str, current: str) -> tuple[str, collections.Counter, list]:
    """Rebuild *current* with safe redactions reverted. Returns (text, counts, samples)."""
    counts: collections.Counter = collections.Counter()
    samples: list = []

    old = None
    for c in MIGRATION_COMMITS:
        old = git_show(f"{c}^", relpath)
        if old is not None:
            break
    if old is None:
        counts["skip:no-pre-image"] += 1
        return current, counts, samples

    sm = difflib.SequenceMatcher(None, old, current, autojunk=False)
    out: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        chunk_new = current[j1:j2]
        if tag in ("equal", "insert"):
            out.append(chunk_new)
            continue
        if tag == "delete":
            continue
        # replace: does the new side consist of exactly one marker?
        if chunk_new.strip() == MARKER:
            old_tok = old[i1:i2]
            cls = classify(old_tok)
            if cls:
                counts[cls] += 1
                if len(samples) < 3:
                    samples.append((old_tok[:60], cls))
                out.append(chunk_new.replace(MARKER, old_tok.strip()))
                continue
            counts["kept:real-pii"] += 1
        out.append(chunk_new)
    return "".join(out), counts, samples


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    # Skip pathologically large files (merge-proposal logs, not person/entity
    # pages) -- difflib.SequenceMatcher(autojunk=False) is a character-level
    # diff and hangs for hours on multi-MB text (_pending_merges_archive.md is
    # 22MB). athenaeum#691's own worked examples are all normal person pages; these
    # logs were never in scope.
    SIZE_LIMIT = 500_000
    skipped_large: list[str] = []
    all_md = list(WIKI.rglob("*.md"))
    pages = []
    for p in sorted(all_md):
        if p.stat().st_size > SIZE_LIMIT:
            text_head = p.read_text(encoding="utf-8", errors="replace")
            if MARKER in text_head:
                skipped_large.append(f"{p.name} ({p.stat().st_size:,} bytes)")
            continue
        if MARKER in p.read_text(encoding="utf-8", errors="replace"):
            pages.append(p)
    if skipped_large:
        print(
            f"  SKIPPED {len(skipped_large)} oversized file(s) "
            f"(>{SIZE_LIMIT:,} bytes), not scanned:"
        )
        for s in skipped_large:
            print(f"    {s}")
    if args.limit:
        pages = pages[: args.limit]

    total: collections.Counter = collections.Counter()
    changed = 0
    all_samples: dict = {}
    for i, p in enumerate(pages, 1):
        rel = str(p.relative_to(KNOWLEDGE))
        cur = p.read_text(encoding="utf-8")
        new, counts, samples = restore_page(rel, cur)
        total.update(counts)
        for tok, cls in samples:
            all_samples.setdefault(cls, tok)
        if new != cur:
            changed += 1
            if args.apply:
                p.write_text(new, encoding="utf-8")
        if i % 200 == 0:
            print(f"  [{i}/{len(pages)}] pages scanned, {changed} would change", file=sys.stderr)

    mode = "APPLIED" if args.apply else "DRY RUN"
    verb = "changed" if args.apply else "would change"
    print(f"\n[{mode}] {len(pages)} pages carried a marker; {changed} {verb}")
    print("\n  restored by class:")
    restored = 0
    for k, n in sorted(total.items()):
        if k.startswith("kept:") or k.startswith("skip:"):
            continue
        restored += n
        print(f"    {k:24} {n:5}   e.g. {all_samples.get(k, '')!r}")
    print(f"    {'TOTAL RESTORED':24} {restored:5}")
    print("\n  deliberately left redacted:")
    for k, n in sorted(total.items()):
        if k.startswith("kept:") or k.startswith("skip:"):
            print(f"    {k:24} {n:5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
