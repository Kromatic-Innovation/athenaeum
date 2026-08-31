# SPDX-License-Identifier: Apache-2.0
"""Deterministic decay sweep for expired ``bucket: daily`` wiki pages (issue athenaeum#904, AC6).

The other half of athenaeum#904's decay-bucket slice: intake/`remember()`/shape-rules
can tag a page ``bucket: daily`` and suggest a ``valid_until`` (`athenaeum.models`,
`athenaeum.mcp_server.remember_write`, `athenaeum.rules`); recall's currency
ranking (`athenaeum.mcp_server._is_deprioritized_for_currency`) deprioritizes
an EXPIRED one so it stops competing with current facts. This module is the
THIRD leg: a periodic, **fully deterministic, zero-LLM-call** sweep that
actually removes an expired daily-bucket page from the live wiki tree —
"a rapidly-overwritten daily status collapses to its latest value plus git
history, instead of N stale pages competing in recall" (issue text).

**No LLM calls — structurally, not just in practice.** Every function in this
module has NO ``client``/``provider``/model parameter anywhere in its
signature — there is nothing here for an LLM call to hang off of. Contrast
with :mod:`athenaeum.merge`'s C4 contradiction detector or the reasoning
tiers, which thread an explicit ``client: LLMBackend | None`` through their
call chains; this module has no such parameter because it makes no such
call. ``tests/test_decay_sweep.py::TestNoLLMCalls`` asserts this via
``inspect.signature``.

**Archive (git-rm), not tombstone.** AC6 says "archives or tombstones" — this
module picks git-rm removal, for three reasons:

1. It is the EXISTING precedent this codebase already uses for exactly this
   shape of removal (:func:`athenaeum.auto_memory_prune.apply_prune`,
   :func:`athenaeum.corrections.retire_batch`, and the freshest example,
   :func:`athenaeum.pending_merges._apply_fold_into_existing`, brought up to
   the two-commit convention in athenaeum#947). Following it here, rather than
   inventing a tombstone shape, is what "do not invent a new one" (the
   athenaeum#904 design brief) asks for.
2. A tombstone (an in-tree stub page, e.g. ``archived: true``) would still be
   a candidate row for the FTS5/vector/keyword index and would need its own
   new filtering logic threaded through every recall backend to keep it from
   ever surfacing as a hit — exactly the "second storage surface" /
   parallel-mechanism sprawl the issue's Out of scope section forbids ("Same
   wiki, marked differently — there is no second store").
3. "Collapses to its latest value plus git history" (the issue's own framing
   of the intended effect) literally describes removal-from-tree +
   git-recoverable history, not an in-tree marker.

**Two-commit + refuse-without-git**, mirroring
:func:`athenaeum.pending_merges._apply_fold_into_existing` exactly (issue
athenaeum#947 is the freshest instance of this discipline in the repo):
Commit A snapshots the kill-list's CURRENT on-disk content (in case a page
was written/edited since its last commit) before anything is touched; Commit
B is the ``git rm`` + removal commit. :func:`apply_sweep` refuses outright —
never degrades to a bare ``Path.unlink()`` — when ``knowledge_root`` is not a
git repository, exactly like :func:`athenaeum.auto_memory_prune.apply_prune`.

**Sweep ledger (issue athenaeum#969, reconciling athenaeum#904 with the memory
model).** The two-commit pattern above is *recoverability*, not a ledger —
"zero destructive operations without a ledger entry" needs its own durable,
append-only record, sibling in shape to :mod:`athenaeum.push_metrics`'s
``_push_records.jsonl`` (JSONL, ``O_APPEND`` + ``fsync``, under the cache
dir, never inside the wiki corpus). :func:`apply_sweep` writes one
:class:`SweepLedgerRecord` per kill-list page — which page, why (bucket +
``valid_until``), the sweep timestamp, and the RECOVERING commit SHA (the
commit whose tree still holds the page's full content, i.e. ``HEAD`` right
after Commit A / its no-op) — via :func:`write_sweep_ledger`, and does so
**before** Commit B ever runs. A ledger-write failure is never swallowed: it
aborts the sweep before ``git rm``, so a page can never be archived without a
durable record of why (see :func:`write_sweep_ledger`'s docstring for why
this, unlike :func:`athenaeum.push_metrics.record_push`, must not be
best-effort). "That-and-why, never content" — the ledger records metadata
about the archival, never the page's body text.

**Sweeps ONLY expired ``daily``-bucket pages** (AC6 is explicit). ``weekly``/
``durable``/unbucketed pages are never touched — this module has no code
path that can select one. "Expired" reuses the EXISTING athenaeum#308
:func:`athenaeum.models.valid_until_expired` predicate — the same one recall's
currency ranking uses — never a parallel validity concept.

Layering: L4 domain/pipeline, mirroring :mod:`athenaeum.auto_memory_prune`
exactly (dry-run-by-default report + separate apply, ``git rm`` in one
labeled commit pair, refuse-without-git).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_cache_dir
from athenaeum.models import parse_bucket, parse_frontmatter, valid_until_expired
from athenaeum.store import append_line_durable

log = logging.getLogger(__name__)

#: Filename under the cache dir. Never under the wiki/raw corpus (issue
#: athenaeum#969 acceptance: "a durable, machine-readable location outside
#: the wiki corpus") — same discipline as ``_push_records.jsonl``
#: (:data:`athenaeum.push_metrics.PUSH_RECORDS_FILENAME`).
SWEEP_LEDGER_FILENAME = "_decay_sweep_records.jsonl"

#: Schema version stamped on every sweep-ledger record.
SWEEP_LEDGER_SCHEMA_VERSION = 1


@dataclass
class SweepCandidate:
    """One expired ``bucket: daily`` page slated for archival, with its reason.

    ``bucket``/``valid_until`` (issue athenaeum#969) carry the same "why" the
    human-readable *reason* string already states, structured for the sweep
    ledger — never parsed back out of *reason* at ledger-write time.

    ``route`` (issue athenaeum#1116 AC3): ``"archive"`` (the original,
    default behavior — git-rm) or ``"off-corpus"`` when the active retention
    pack is authoritative for this page's ``(memory_class, data_class)`` and
    resolves to an erasure-class action (``store-off-corpus`` /
    ``refuse-write``) — see :func:`build_sweep_report`'s docstring.
    """

    path: Path
    reason: str
    bucket: str = "daily"
    valid_until: str | None = None
    route: str = "archive"


@dataclass
class SweepReport:
    """Outcome of a sweep pass (dry-run or apply)."""

    kill: list[SweepCandidate] = field(default_factory=list)
    retained: list[tuple[Path, str]] = field(default_factory=list)
    scanned: int = 0
    applied: bool = False
    committed: bool = False
    errors: list[str] = field(default_factory=list)
    #: Issue athenaeum#1116 AC3: pages routed to the off-corpus surface rather
    #: than git-rm archived, because the active retention pack is
    #: authoritative for their ``(memory_class, data_class)`` pair. Disjoint
    #: from ``kill`` (`apply_sweep` never git-rms one of these AS a plain
    #: archive — see that function).
    routed_off_corpus: list[SweepCandidate] = field(default_factory=list)


@dataclass
class SweepLedgerRecord:
    """One archived-page ledger row (issue athenaeum#969, AC1).

    "That-and-why, never content": every field is an identifier, a
    classification token, or a timestamp — never the page's body text.
    """

    page: str
    bucket: str
    valid_until: str | None
    swept_at: str
    recovering_commit: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": SWEEP_LEDGER_SCHEMA_VERSION,
            "page": self.page,
            "bucket": self.bucket,
            "valid_until": self.valid_until,
            "swept_at": self.swept_at,
            "recovering_commit": self.recovering_commit,
        }


def discover_daily_bucket_pages(wiki_root: Path) -> list[Path]:
    """Return sorted wiki pages carrying ``bucket: daily`` in frontmatter.

    Shallow scan (mirrors :mod:`athenaeum.auto_memory_prune`'s ``wiki/*.md``
    convention) — the underscore-prefixed operational subtree
    (``_pending_questions.md`` etc.) is never a candidate.
    """
    if not wiki_root.is_dir():
        return []
    candidates: list[Path] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        if parse_bucket(meta) == "daily":
            candidates.append(path)
    return candidates


def build_sweep_report(
    wiki_root: Path,
    *,
    as_of: date | None = None,
    knowledge_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> SweepReport:
    """Classify every ``bucket: daily`` page into kill vs retained (issue athenaeum#904, AC6).

    A page joins the kill-list only when it is BOTH ``bucket: daily`` AND
    expired (:func:`athenaeum.models.valid_until_expired` against *as_of*,
    default today) — a daily-bucket page with no ``valid_until`` (or one not
    yet passed) is retained, exactly matching the fail-open athenaeum#308 posture
    ("absent valid_until => open upper bound => currently valid").

    **Pack authority (issue athenaeum#1116 AC3, `docs/provenance-shape.md`
    §8.8).** When a page's frontmatter names BOTH ``memory_class`` and
    ``data_class`` (the latter is not written by any shipped write path as
    of this issue — this is the extension point for whenever one starts
    stamping it, e.g. after an AC1/AC2 off-corpus routing decision), the
    active retention pack is consulted (:func:`athenaeum.erasure.reconcile_bucket_daily_with_pack`)
    BEFORE the independent ``bucket: daily`` / ``valid_until`` logic above
    runs at all. When the pack resolves an erasure-class action
    (``store-off-corpus`` / ``refuse-write`` —
    :meth:`athenaeum.erasure.RetentionRule.is_erasure_class`), the page is
    pack-authoritative and joins ``routed_off_corpus`` regardless of
    ``valid_until`` — placement, not expiry, is what that action means (a
    page that must never live in the ordinary corpus does not get to wait
    out its ``valid_until`` there first). A period-bearing action
    (``delete-after``/``retain-until``) does NOT change this function's
    behavior: §8.8 is explicit that a pack's ``delete-after`` period is "the
    same window ``bucket: daily``'s ``valid_until`` already encodes today...
    packs do not introduce a second expiry clock", so such a page falls
    through to the existing ``valid_until_expired`` check unchanged, same as
    a page with no ``data_class`` at all.

    Requires a real off-corpus surface to route to
    (:func:`athenaeum.off_corpus.off_corpus_store` against *knowledge_root*,
    default ``wiki_root.parent``, and *config*) — when none is configured
    (the common case today), pack consultation is skipped entirely and this
    function's behavior is BYTE-IDENTICAL to before this issue: no existing
    write path stamps ``data_class`` yet, so this gate does not fire on any
    corpus produced by shipped code regardless.

    Makes ZERO LLM calls — no ``client``/model parameter exists on this
    function to make one with (see module docstring).
    """
    from athenaeum.off_corpus import off_corpus_store

    kroot = knowledge_root if knowledge_root is not None else wiki_root.parent
    off_corpus_configured = off_corpus_store(config, kroot) is not None

    report = SweepReport()
    for path in discover_daily_bucket_pages(wiki_root):
        report.scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append(f"{path.name}: unreadable ({exc})")
            report.retained.append((path, "unreadable - retained for safety"))
            continue
        meta, _body = parse_frontmatter(text)

        memory_class = meta.get("memory_class")
        data_class = meta.get("data_class")
        if off_corpus_configured and isinstance(memory_class, str) and isinstance(
            data_class, str
        ):
            from athenaeum.erasure import (
                reconcile_bucket_daily_with_pack,
                resolve_active_retention_pack,
            )

            pack = resolve_active_retention_pack(config)
            rule = reconcile_bucket_daily_with_pack(
                memory_class=memory_class, data_class=data_class, pack=pack
            )
            if rule.is_erasure_class():
                report.routed_off_corpus.append(
                    SweepCandidate(
                        path,
                        f"retention pack {pack.name!r} authoritative for "
                        f"(memory_class={memory_class!r}, data_class={data_class!r}): "
                        f"{rule.action}",
                        bucket="daily",
                        valid_until=(
                            str(meta["valid_until"]) if meta.get("valid_until") else None
                        ),
                        route="off-corpus",
                    )
                )
                continue
        elif not off_corpus_configured and isinstance(memory_class, str) and isinstance(
            data_class, str
        ):
            log.warning(
                "erasure-taint-not-routed: %s carries memory_class=%r data_class=%r "
                "but no off-corpus surface is configured (off_corpus.enabled=false) "
                "- falling back to bucket:daily/valid_until sweep logic (athenaeum#1116)",
                path.name,
                memory_class,
                data_class,
            )

        if valid_until_expired(meta, as_of):
            raw_valid_until = meta.get("valid_until")
            reason = f"bucket: daily, expired (valid_until={raw_valid_until!r})"
            report.kill.append(
                SweepCandidate(
                    path,
                    reason,
                    bucket="daily",
                    valid_until=str(raw_valid_until) if raw_valid_until else None,
                )
            )
        else:
            report.retained.append((path, "bucket: daily, not yet expired"))
    return report


def sweep_ledger_path(cache_dir: Path | None = None) -> Path:
    """Resolve the sweep-ledger path: ``<cache_dir>/_decay_sweep_records.jsonl``.

    Same resolver as every other cache-dir ledger
    (:func:`athenaeum.push_metrics.push_records_path`): ``arg >
    ATHENAEUM_CACHE_DIR env > ~/.cache/athenaeum`` — durable, and outside the
    wiki corpus by construction.
    """
    return resolve_cache_dir(cache_dir) / SWEEP_LEDGER_FILENAME


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _append_ledger_line(path: Path, line: str) -> None:
    """Append one line durably (``O_APPEND`` + ``fsync``), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy onto
    (design note §2.4 / §6.2).

    One difference stays in the CALLER's contract, not this function's body:
    :func:`write_sweep_ledger` (unlike :func:`athenaeum.push_metrics.record_push`)
    does NOT catch and swallow a write failure here — see that docstring for
    why a ledger write on this path must be allowed to fail loudly.
    """
    append_line_durable(path, line.encode("utf-8"))


def write_sweep_ledger(
    records: list[SweepLedgerRecord],
    *,
    cache_dir: Path | None = None,
) -> None:
    """Append *records* to the durable sweep ledger. Raises on failure.

    Deliberately NOT best-effort, unlike every other ledger writer in this
    codebase (:func:`athenaeum.push_metrics.record_push`,
    :func:`athenaeum.push_metrics.record_reference_result`): those protect a
    live, non-destructive read path from ever breaking because telemetry
    failed to write. This ledger sits upstream of a DESTRUCTIVE operation
    (:func:`apply_sweep`'s ``git rm``) — issue athenaeum#969 AC1 requires the
    sweep to REFUSE to archive when the ledger write fails, which is only
    possible if the failure propagates to the caller instead of being logged
    and swallowed. Callers that want best-effort semantics must catch this
    themselves; :func:`apply_sweep` deliberately does not.
    """
    path = sweep_ledger_path(cache_dir)
    lines = "".join(
        json.dumps(rec.to_dict(), separators=(",", ":")) + "\n" for rec in records
    )
    _append_ledger_line(path, lines)


def read_sweep_ledger(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read every sweep-ledger record. Tolerates a torn trailing line; never raises."""
    path = sweep_ledger_path(cache_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with ``cwd=root``. ``check=False`` — callers inspect
    ``.returncode`` themselves (matches :func:`athenaeum.corrections._git` /
    :func:`athenaeum.pending_merges._git`, both of which need this because
    ``git diff --cached --quiet``'s DELIBERATE nonzero-on-diff exit code
    would otherwise raise on the exact call this module needs to inspect,
    not treat as a failure).
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )


def _apply_off_corpus_routing(
    knowledge_root: Path,
    report: SweepReport,
    *,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
) -> None:
    """Move each ``report.routed_off_corpus`` candidate off-corpus, then
    ``git rm`` the ordinary-corpus copy (issue athenaeum#1116 AC3) — the
    placement half of §8.8's "once a pack exists, it is authoritative".
    Mutates *report*.

    Deliberately a SEPARATE code path from :func:`apply_sweep`'s kill-list
    ``git rm`` below, not a shared refactor of it — this issue is a wiring
    slice; the existing, already-tested archive flow (issues athenaeum#904 and
    athenaeum#969) is left untouched. Mirrors its Commit A (provenance snapshot) / ledger /
    Commit B discipline exactly, so a routed page is exactly as recoverable
    from git history as an archived one, and never removed from the
    ordinary corpus without BOTH a successful off-corpus write and a
    durable ledger record first.
    """
    if not report.routed_off_corpus:
        return

    from athenaeum.off_corpus import off_corpus_adapter, off_corpus_store
    from athenaeum.store import StoreKey

    store = off_corpus_store(config, knowledge_root)
    if store is None:
        # Config changed between build_sweep_report and apply_sweep (or a
        # caller built the report by hand) — refuse rather than doing
        # anything destructive with nowhere to route to.
        msg = "off-corpus surface not configured at apply time - not routing"
        log.warning("decay-sweep: %s", msg)
        report.errors.append(msg)
        return
    adapter = off_corpus_adapter(config)
    assert adapter is not None  # off_corpus_store already returned non-None

    if not (knowledge_root / ".git").exists():
        msg = (
            f"no .git in {knowledge_root} - refusing to sweep (archival is "
            "git-only for recoverability, issue athenaeum#904 AC7)"
        )
        log.warning("decay-sweep: %s", msg)
        report.errors.append(msg)
        return

    kr = knowledge_root.resolve()
    pairs: list[tuple[SweepCandidate, str]] = []
    for cand in report.routed_off_corpus:
        try:
            rel = str(cand.path.resolve().relative_to(kr))
        except ValueError:
            report.errors.append(f"{cand.path.name}: outside knowledge_root - not routed")
            continue
        pairs.append((cand, rel))
    if not pairs:
        return
    rel_paths = [rel for _, rel in pairs]

    # Commit A — provenance snapshot BEFORE any removal, same convention as
    # the kill-list flow below.
    add_result = _git(knowledge_root, "add", "--", *rel_paths)
    if add_result.returncode != 0:
        msg = f"git add failed while routing off-corpus: {add_result.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return
    staged = _git(knowledge_root, "diff", "--cached", "--quiet", "--", *rel_paths)
    if staged.returncode != 0:
        commit_a = _git(
            knowledge_root,
            "commit",
            "-m",
            f"chore(decay-sweep): provenance snapshot before routing "
            f"{len(rel_paths)} page(s) off-corpus (athenaeum#1116)",
            "--",
            *rel_paths,
        )
        if commit_a.returncode != 0:
            msg = f"provenance-snapshot commit failed: {commit_a.stderr.strip()}"
            log.error("decay-sweep: %s", msg)
            report.errors.append(msg)
            return

    head_result = _git(knowledge_root, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        msg = f"could not resolve recovering commit SHA: {head_result.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return
    recovering_sha = head_result.stdout.strip()

    # Off-corpus write BEFORE removal from the ordinary corpus — never a
    # window where the content exists in neither place.
    contents: dict[str, bytes] = {}
    for cand, rel in pairs:
        try:
            contents[rel] = cand.path.read_bytes()
        except OSError as exc:
            msg = f"{rel}: unreadable, not routed ({exc})"
            log.error("decay-sweep: %s", msg)
            report.errors.append(msg)
    pairs = [(cand, rel) for cand, rel in pairs if rel in contents]
    if not pairs:
        return
    rel_paths = [rel for _, rel in pairs]
    for rel in rel_paths:
        store.put(StoreKey(surface=adapter.name, key=rel), contents[rel])

    # Ledger write BEFORE removal from the ordinary corpus (same fail-closed
    # ordering as the kill-list flow's issue athenaeum#969 AC1 discipline).
    swept_at = _now_iso()
    ledger_records = [
        SweepLedgerRecord(
            page=rel,
            bucket=cand.bucket,
            valid_until=cand.valid_until,
            swept_at=swept_at,
            recovering_commit=recovering_sha,
        )
        for cand, rel in pairs
    ]
    try:
        write_sweep_ledger(ledger_records, cache_dir=cache_dir)
    except Exception as exc:  # noqa: BLE001 — must abort routing, never proceed past it
        msg = (
            f"sweep-ledger write failed ({type(exc).__name__}): {exc} - "
            "refusing to remove from the ordinary corpus (athenaeum#1116)"
        )
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return

    rm_result = _git(knowledge_root, "rm", "--quiet", "--", *rel_paths)
    if rm_result.returncode != 0:
        msg = f"git rm failed while routing off-corpus: {rm_result.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return
    commit_b = _git(
        knowledge_root,
        "commit",
        "-m",
        f"chore(decay-sweep): route {len(rel_paths)} page(s) off-corpus "
        f"(pack-authoritative, athenaeum#1116)",
        "--",
        *rel_paths,
    )
    if commit_b.returncode != 0:
        msg = f"off-corpus routing commit failed: {commit_b.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return
    report.applied = True
    report.committed = True
    log.info(
        "decay-sweep: routed %d page(s) off-corpus (pack-authoritative, "
        "athenaeum#1116); committed",
        len(rel_paths),
    )


def apply_sweep(
    knowledge_root: Path,
    report: SweepReport,
    *,
    cache_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> SweepReport:
    """Archive the kill-list via a two-commit git-rm (issue athenaeum#904, AC6/AC7).

    Mirrors :func:`athenaeum.pending_merges._apply_fold_into_existing`'s
    Commit A (provenance snapshot) / Commit B (``git rm`` + removal) shape —
    see the module docstring for why this convention rather than a new one.
    Refuses to act (never degrades to a bare ``unlink``, exactly like
    :func:`athenaeum.auto_memory_prune.apply_prune`) when *knowledge_root* is
    not a git repository. A no-op (no commit) when the kill-list is empty.
    Mutates and returns *report*.

    Issue athenaeum#969 AC1: between Commit A and Commit B, this function
    writes one durable ledger record per kill-list page
    (:func:`write_sweep_ledger`, under ``cache_dir`` /
    :func:`sweep_ledger_path`) and REFUSES to run Commit B — the actual
    ``git rm`` — when that write fails. *cache_dir* threads through to the
    ledger resolver exactly like every other cache-dir-rooted artifact
    (``arg > ATHENAEUM_CACHE_DIR env > ~/.cache/athenaeum``).

    Issue athenaeum#1116 AC3: ``report.routed_off_corpus`` (built by
    :func:`build_sweep_report` when the active retention pack is
    authoritative for a page) is applied FIRST, via
    :func:`_apply_off_corpus_routing` — a completely separate code path
    from the kill-list archive below, since a pack-authoritative page is
    never git-rm "archived", it is MOVED off-corpus. *config* threads
    through to that routing only; the kill-list archive below does not
    consult retention packs at all (see :func:`build_sweep_report`).
    """
    _apply_off_corpus_routing(knowledge_root, report, config=config, cache_dir=cache_dir)

    if not report.kill:
        log.info("decay-sweep: kill-list empty - nothing to archive")
        return report

    if not (knowledge_root / ".git").exists():
        msg = (
            f"no .git in {knowledge_root} - refusing to sweep (archival is "
            "git-only for recoverability, issue athenaeum#904 AC7)"
        )
        log.warning("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report

    kr = knowledge_root.resolve()
    pairs: list[tuple[SweepCandidate, str]] = []
    for cand in report.kill:
        try:
            rel = str(cand.path.resolve().relative_to(kr))
        except ValueError:
            report.errors.append(
                f"{cand.path.name}: outside knowledge_root - not swept"
            )
            continue
        pairs.append((cand, rel))
    if not pairs:
        return report
    rel_paths = [rel for _, rel in pairs]

    # Commit A — provenance snapshot BEFORE any removal (issue athenaeum#947
    # convention): stages exactly the kill-list paths (never `git add -A`, so
    # an operator's unrelated pre-staged work is never swept in under this
    # commit's message) and commits only if something is actually staged —
    # the common case, a page already fully committed from a prior run, is a
    # legitimate no-op here, not an error.
    add_result = _git(knowledge_root, "add", "--", *rel_paths)
    if add_result.returncode != 0:
        msg = f"git add failed during decay sweep: {add_result.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report
    staged = _git(knowledge_root, "diff", "--cached", "--quiet", "--", *rel_paths)
    if staged.returncode != 0:
        commit_a = _git(
            knowledge_root,
            "commit",
            "-m",
            f"chore(decay-sweep): provenance snapshot before archiving "
            f"{len(rel_paths)} expired daily-bucket page(s) (athenaeum#904)",
            "--",
            *rel_paths,
        )
        if commit_a.returncode != 0:
            msg = f"provenance-snapshot commit failed: {commit_a.stderr.strip()}"
            log.error("decay-sweep: %s", msg)
            report.errors.append(msg)
            return report

    # The recovering commit SHA (issue athenaeum#969): the commit whose tree
    # still holds every kill-list page's full content. This is HEAD at this
    # exact point — either Commit A just made it so (a page edited since its
    # last commit), or Commit A was a legitimate no-op because HEAD already
    # carries the page byte-for-byte (the common case). Either way, `git show
    # <this-sha>:<rel_path>` recovers the page; Commit B (below) is what
    # makes that necessary.
    head_result = _git(knowledge_root, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        msg = f"could not resolve recovering commit SHA: {head_result.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report
    recovering_sha = head_result.stdout.strip()

    # Ledger write BEFORE archival (issue athenaeum#969 AC1, fail-closed
    # ordering): a ledger-write failure aborts HERE, before `git rm` ever
    # runs, so a page can never be archived without a durable record of why.
    # Deliberately not try/except-and-continue past this — see
    # `write_sweep_ledger`'s docstring.
    swept_at = _now_iso()
    ledger_records = [
        SweepLedgerRecord(
            page=rel,
            bucket=cand.bucket,
            valid_until=cand.valid_until,
            swept_at=swept_at,
            recovering_commit=recovering_sha,
        )
        for cand, rel in pairs
    ]
    try:
        write_sweep_ledger(ledger_records, cache_dir=cache_dir)
    except Exception as exc:  # noqa: BLE001 — must abort archival, never proceed past it
        msg = (
            f"sweep-ledger write failed ({type(exc).__name__}): {exc} - "
            "refusing to archive (issue athenaeum#969 AC1)"
        )
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report

    # Commit B — the archival itself.
    rm_result = _git(knowledge_root, "rm", "--quiet", "--", *rel_paths)
    if rm_result.returncode != 0:
        msg = f"git rm failed during decay sweep: {rm_result.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report
    commit_b = _git(
        knowledge_root,
        "commit",
        "-m",
        f"chore(decay-sweep): archive {len(rel_paths)} expired "
        f"daily-bucket page(s) (athenaeum#904)",
        "--",
        *rel_paths,
    )
    if commit_b.returncode != 0:
        msg = f"archive commit failed: {commit_b.stderr.strip()}"
        log.error("decay-sweep: %s", msg)
        report.errors.append(msg)
        return report
    report.applied = True
    report.committed = True
    log.info(
        "decay-sweep: git-archived %d expired daily-bucket page(s); committed",
        len(rel_paths),
    )
    return report
