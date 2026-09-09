# SPDX-License-Identifier: Apache-2.0
"""Enforceable retention policy for preserved source logs (issue athenaeum#1418).

The detect-only half of this story already exists and is untouched by this
module: :func:`athenaeum.config.resolve_raw_retention_max_file_bytes` /
``max_source_bytes`` (issue athenaeum#1269) REPORT a raw-intake file or source
tree crossing a size ceiling and never act on it (AC7). This module is the
enforcement half the issue asks for, but for a DIFFERENT tree: not raw
intake, but a **preserved log** -- a source index the operator has already
chosen to keep whole via the ``preserve`` shape-rule disposition
(:mod:`athenaeum.rules`, issue athenaeum#837), living under
``librarian.preserved_log_dir`` / ``librarian.preserved_log_adapter``. Those
grow unboundedly today (the issue cites ``_pending_questions.md`` at 20.6 MB
as evidence of the *pattern* -- explicitly NOT a target here, since that file
is an internal machinery ledger, out of scope per the issue).

**Three policies** (:func:`athenaeum.config.resolve_retention_policy`):
``truncate-top`` drops the OLDEST entries once a family's log exceeds its
configured ``max_bytes`` bound; ``never-truncate`` is an explicit, recorded
choice to stay unbounded (never conflated with "no policy configured" --
that reads as ``None``, AC3); ``librarian-decides`` delegates the purge
decision to an injected callable (the reasoning-tier integration seam, kept
OUT of this module's own dependency graph -- see
:func:`apply_librarian_decides`'s docstring for why) and always logs a
spend record, even a zero one (AC4).

**Prior art this module follows, not reinvents** (per the issue's own
"Prior art to follow" section): :mod:`athenaeum.decay_sweep`'s discipline --
fully deterministic for the mechanical policies (no LLM client parameter
anywhere in :func:`plan_truncate_top`'s call chain), a two-commit git
sequence (Commit A snapshots current content BEFORE any mutation, Commit B
is the truncating rewrite), an append-only ledger written to the SAME
:func:`athenaeum.config.resolve_cache_dir` tree decay_sweep's ledger lives
under (never inside the wiki corpus) and written BEFORE Commit B -- a
ledger-write failure aborts the whole operation rather than truncating with
no record -- and an outright refusal to run against a *knowledge_root* that
is not a git repository (never a bare ``Path.write_text`` fallback).

**AC6 -- the safety-critical one.** Before anything is dropped, every
candidate line is checked against :func:`find_cited_locators`, which scans
the live wiki tree for the literal ``preserved-log:<path>#L<n>`` pointer
(:data:`athenaeum.rules.PRESERVED_LOG_SOURCE_SCHEME` -- the SAME scheme a
compiled fact's ``source.ref`` already uses, issue athenaeum#837/#1132, so a
structured citation and an inline footnote citing the pointer are both
caught by one substring scan of each page's raw text). A cited candidate is
never dropped -- it is excluded from the drop set (reported in
:attr:`RetentionOutcome.retained_cited`) even if that means the file stays
over its configured bound. "Preserved or reported" (AC6's own wording) is
satisfied twice over for a dropped-but-uncited entry too: its full content
is archived at the resolved *destination* (:func:`resolve_destination_root`)
BEFORE the live file is rewritten, and its metadata (never its content --
"that-and-why, never content", the same principle
:class:`athenaeum.decay_sweep.SweepLedgerRecord` states) is written to the
retention ledger.

**AC5 -- destination reuses the two existing exit points, plus one more.**
``in-repo`` resolves through :func:`athenaeum.config.resolve_preserved_log_dir`
exactly like the ``preserve`` disposition does; ``adapter:<name>`` resolves
through :func:`athenaeum.storage.available_adapters` exactly like
``preserved_log_adapter`` does (same fail-loud
:class:`~athenaeum.storage.StorageConfigError` on an unknown name);
``pii-vault`` resolves to :data:`athenaeum.storage.EXCLUDED`'s root -- the
SAME built-in "excluded" surface :mod:`athenaeum.sensitivity_routing` already
calls the secret vault. No fourth path is opened: every branch ends in
:meth:`athenaeum.storage.StorageAdapter.resolve_root`.

Layering: L4 domain/pipeline module, a peer of :mod:`athenaeum.decay_sweep`.
Imports :mod:`athenaeum.config` (L2), :mod:`athenaeum.storage` (L1),
:mod:`athenaeum.store` (L0/L1), :mod:`athenaeum.spend` (L3, for the
fleet-format record shape only -- :func:`athenaeum.spend.build_record`, never
:func:`athenaeum.spend.record_spend`, since that function deliberately
no-ops a zero-usage call and AC4 requires the opposite) and
:mod:`athenaeum.models` (L1, for :class:`~athenaeum.models.TokenUsage`), plus
:mod:`athenaeum.rules` (L3, for the ``PRESERVED_LOG_SOURCE_SCHEME`` constant
only) -- all at or below L4, so this module never widens the import graph
upward.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from athenaeum.atomic_io import atomic_write_text
from athenaeum.config import (
    resolve_cache_dir,
    resolve_preserved_log_dir,
    resolve_retention_destination,
    resolve_retention_max_bytes,
    resolve_retention_policy,
)
from athenaeum.models import TokenUsage
from athenaeum.rules import PRESERVED_LOG_SOURCE_SCHEME
from athenaeum.spend import build_record
from athenaeum.storage import EXCLUDED, available_adapters
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

#: Filename under the cache dir -- never under the wiki/raw corpus, same
#: discipline as :data:`athenaeum.decay_sweep.SWEEP_LEDGER_FILENAME` (AC2).
RETENTION_LEDGER_FILENAME = "_retention_records.jsonl"

#: Schema version stamped on every retention-ledger record.
RETENTION_LEDGER_SCHEMA_VERSION = 1

#: ``run_type`` this module's spend records carry (AC4, "fleet format").
#: Prefixed ``librarian-`` so :func:`athenaeum.spend.is_librarian_run_type`
#: classifies it as a member of the librarian family with no change to that
#: function.
LIBRARIAN_DECIDES_RUN_TYPE = "librarian-retention-decides"


class RetentionConfigError(ValueError):
    """Raised when a retention *destination* names an unresolvable surface
    (an unknown ``adapter:<name>``, or ``in-repo`` with no
    ``preserved_log_dir`` configured) -- fail loud, mirroring
    :class:`athenaeum.storage.StorageConfigError`, never a silent fallback
    that would leave a dropped entry with nowhere recoverable to land.
    """


@dataclass
class RetentionEntry:
    """One line of a preserved log, addressed the same way a compiled fact's
    ``source.ref`` already addresses it (issue athenaeum#837): ``index`` is
    0-based; :attr:`locator` is the 1-based ``L<n>`` form the
    ``preserved-log:`` pointer scheme uses.
    """

    index: int
    raw_line: str
    byte_length: int

    @property
    def locator(self) -> str:
        return f"L{self.index + 1}"


@dataclass
class RetentionCandidate:
    """One entry slated for removal, with why -- and whether a live citation
    protected it (AC6)."""

    entry: RetentionEntry
    reason: str
    cited: bool = False


@dataclass
class RetentionOutcome:
    """Outcome of one :func:`apply_retention` (or
    :func:`apply_librarian_decides`) call."""

    family: str
    log_path: Path
    policy: str | None
    scanned: int = 0
    total_bytes: int = 0
    max_bytes: int | None = None
    dropped: list[RetentionCandidate] = field(default_factory=list)
    #: Would-be-dropped candidates a live ``preserved-log:`` citation
    #: protected (AC6) -- excluded from `dropped`, never removed.
    retained_cited: list[RetentionCandidate] = field(default_factory=list)
    applied: bool = False
    committed: bool = False
    archive_path: Path | None = None
    spend_record: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class RetentionLedgerRecord:
    """One dropped-entry ledger row (AC2). "That-and-why, never content" --
    ``content_sha256``/``byte_length`` identify what was dropped without
    ever writing its (possibly PII-bearing, per the issue's own
    ``_pending_questions.md`` example) text into the ledger.
    """

    family: str
    log_path: str
    locator: str
    byte_length: int
    content_sha256: str
    reason: str
    policy: str
    swept_at: str
    recovering_commit: str
    archive_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": RETENTION_LEDGER_SCHEMA_VERSION,
            "family": self.family,
            "log_path": self.log_path,
            "locator": self.locator,
            "byte_length": self.byte_length,
            "content_sha256": self.content_sha256,
            "reason": self.reason,
            "policy": self.policy,
            "swept_at": self.swept_at,
            "recovering_commit": self.recovering_commit,
            "archive_path": self.archive_path,
        }


def retention_ledger_path(cache_dir: Path | None = None) -> Path:
    """``<cache_dir>/_retention_records.jsonl`` -- durable, outside the wiki
    corpus by construction, same resolver shape as
    :func:`athenaeum.decay_sweep.sweep_ledger_path`.
    """
    return resolve_cache_dir(cache_dir) / RETENTION_LEDGER_FILENAME


def _append_ledger_line(path: Path, line: str) -> None:
    append_line_durable(path, line.encode("utf-8"))


def write_retention_ledger(
    records: list[RetentionLedgerRecord], *, cache_dir: Path | None = None
) -> None:
    """Append *records* to the durable retention ledger. Raises on failure.

    Deliberately NOT best-effort -- mirrors
    :func:`athenaeum.decay_sweep.write_sweep_ledger` exactly: this sits
    upstream of a destructive rewrite, so a write failure must propagate and
    abort the caller rather than being logged and swallowed (AC2).
    """
    path = retention_ledger_path(cache_dir)
    lines = "".join(json.dumps(r.to_dict(), separators=(",", ":")) + "\n" for r in records)
    _append_ledger_line(path, lines)


def read_retention_ledger(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read every retention-ledger record. Tolerates a torn trailing line;
    never raises."""
    path = retention_ledger_path(cache_dir)
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


def build_entries(log_path: Path) -> list[RetentionEntry]:
    """Read *log_path* into one :class:`RetentionEntry` per line, oldest
    (top of file) first -- the append-only convention every cited precedent
    in the issue (cluster JSONL rotations, ``_shape_rule_dispositions.jsonl``)
    already uses. Returns ``[]`` for a missing or empty file.
    """
    if not log_path.is_file():
        return []
    text = log_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    return [
        RetentionEntry(index=i, raw_line=line, byte_length=len(line.encode("utf-8")) + 1)
        for i, line in enumerate(lines)
    ]


_CITATION_LOCATOR_RE_TEMPLATE = re.escape(PRESERVED_LOG_SOURCE_SCHEME) + r":{path}#L(\d+)"


def find_cited_locators(wiki_root: Path, log_relpath: str) -> set[str]:
    """Scan every live, non-underscore ``wiki/*.md`` page (shallow, same
    convention as :func:`athenaeum.decay_sweep.discover_daily_bucket_pages`)
    for a ``preserved-log:<log_relpath>#L<n>`` pointer and return the set of
    cited locators (``{"L1", "L42", ...}``).

    One substring/regex scan catches BOTH citation shapes AC6 names: a
    compiled fact's structured ``source.ref`` (frontmatter/JSON embedded
    verbatim in the page's raw markdown) and a prose footnote, because
    either one must contain this exact, greppable pointer string to BE a
    citation at all -- the pointer format is deliberately "greppable in a
    page's ``field_sources``" (issue athenaeum#837's own design note), and
    that property is what this function relies on rather than parsing
    frontmatter and footnote syntax as two separate cases.
    """
    if not wiki_root.is_dir():
        return set()
    pattern = re.compile(_CITATION_LOCATOR_RE_TEMPLATE.format(path=re.escape(log_relpath)))
    cited: set[str] = set()
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in pattern.finditer(text):
            cited.add(f"L{match.group(1)}")
    return cited


def _relpath_for_pointer(knowledge_root: Path, path: Path) -> str:
    """Same fallback shape as
    :func:`athenaeum.rules.preserved_log_source_pointer`: knowledge-root-
    relative when possible, else the path's own absolute POSIX form."""
    try:
        return path.resolve().relative_to(knowledge_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def plan_truncate_top(
    entries: list[RetentionEntry],
    *,
    max_bytes: int,
    cited_locators: set[str],
) -> tuple[list[RetentionCandidate], list[RetentionCandidate]]:
    """Decide which *entries* to drop, oldest-first, to bring the total under
    *max_bytes* -- and which candidates a citation protects instead (AC6).

    Fully deterministic: no client/model parameter anywhere in this
    function's signature (mirrors :mod:`athenaeum.decay_sweep`'s "no LLM
    calls, structurally" contract for its own mechanical policies). Returns
    ``(drop, retained_cited)``. A cited entry is skipped (never counted
    against the bound) rather than dropped -- so the file MAY remain over
    *max_bytes* when enough of its oldest entries are cited; that is the
    correct, safe outcome (AC6 outranks AC2's bound), and every such entry
    is reported in `retained_cited`.
    """
    total = sum(e.byte_length for e in entries)
    remaining = total
    drop: list[RetentionCandidate] = []
    retained_cited: list[RetentionCandidate] = []
    for entry in entries:
        if remaining <= max_bytes:
            break
        if entry.locator in cited_locators:
            retained_cited.append(
                RetentionCandidate(
                    entry,
                    reason=f"would drop oldest-first for truncate-top, but "
                    f"{entry.locator} is cited by a live preserved-log: "
                    "reference (issue athenaeum#1418 AC6)",
                    cited=True,
                )
            )
            continue
        drop.append(RetentionCandidate(entry, reason="truncate-top: oldest past the bound"))
        remaining -= entry.byte_length
    return drop, retained_cited


def resolve_destination_root(
    config: dict[str, Any] | None, family: str, knowledge_root: Path
) -> Path:
    """Resolve *family*'s configured destination (issue athenaeum#1418 AC5) to
    an absolute on-disk root, reusing the two existing preserved-log exit
    points plus the built-in PII vault surface -- never a fourth write path.

    Raises :class:`RetentionConfigError` when the destination cannot be
    resolved to anywhere durable: ``"in-repo"`` with no
    ``librarian.preserved_log_dir`` configured, or ``"adapter:<name>"``
    naming an adapter :func:`athenaeum.storage.available_adapters` does not
    know. Callers must treat this as a reason to REFUSE to drop anything
    (fail closed) rather than fall back to some other location.
    """
    destination = resolve_retention_destination(config, family)
    if destination == "pii-vault":
        return EXCLUDED.resolve_root(knowledge_root)
    if destination.startswith("adapter:"):
        name = destination[len("adapter:") :]
        adapters = available_adapters(config)
        if name not in adapters:
            raise RetentionConfigError(
                f"librarian.retention destination {destination!r} for family "
                f"{family!r} names an unknown storage adapter; known "
                f"adapters: {sorted(adapters)} (issue athenaeum#1418)"
            )
        return adapters[name].resolve_root(knowledge_root)
    # destination == "in-repo"
    preserved_dir = resolve_preserved_log_dir(config)
    if preserved_dir is None:
        raise RetentionConfigError(
            "librarian.retention destination 'in-repo' for family "
            f"{family!r} has no librarian.preserved_log_dir configured -- "
            "refusing to drop anything with nowhere durable to archive it "
            "(issue athenaeum#1418)"
        )
    return knowledge_root / preserved_dir


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` with ``cwd=root``. Mirrors
    :func:`athenaeum.decay_sweep._git` exactly (``check=False`` -- callers
    inspect ``.returncode`` themselves, needed for `git diff --cached
    --quiet`'s deliberate nonzero-on-diff exit code)."""
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=False
    )


def _apply_drop_set(
    knowledge_root: Path,
    log_path: Path,
    entries: list[RetentionEntry],
    drop: list[RetentionCandidate],
    *,
    family: str,
    policy: str,
    config: dict[str, Any] | None,
    cache_dir: Path | None,
    outcome: RetentionOutcome,
) -> None:
    """Shared apply path for both `truncate-top` and `librarian-decides`
    (issue athenaeum#1418): refuse without git, Commit A (provenance
    snapshot), ledger write BEFORE any mutation, archive write BEFORE the
    live rewrite, live rewrite, Commit B. Mutates *outcome*.

    Exactly mirrors :func:`athenaeum.decay_sweep.apply_sweep`'s ordering --
    see that function's docstring for why each step comes where it does.
    """
    if not drop:
        return
    if not (knowledge_root / ".git").exists():
        msg = (
            f"no .git in {knowledge_root} - refusing to truncate (archival "
            "is git-only for recoverability, issue athenaeum#1418, matching "
            "athenaeum#904 AC7's decay-sweep precedent)"
        )
        log.warning("retention: %s", msg)
        outcome.errors.append(msg)
        return

    try:
        rel_log = str(log_path.resolve().relative_to(knowledge_root.resolve()))
    except ValueError:
        msg = f"{log_path}: outside knowledge_root - not truncated"
        outcome.errors.append(msg)
        return

    try:
        destination_root = resolve_destination_root(config, family, knowledge_root)
    except RetentionConfigError as exc:
        outcome.errors.append(str(exc))
        return

    # Commit A -- provenance snapshot BEFORE any removal (issue athenaeum#947
    # convention), staging only the log file itself.
    add_result = _git(knowledge_root, "add", "--", rel_log)
    if add_result.returncode != 0:
        msg = f"git add failed during retention truncation: {add_result.stderr.strip()}"
        log.error("retention: %s", msg)
        outcome.errors.append(msg)
        return
    staged = _git(knowledge_root, "diff", "--cached", "--quiet", "--", rel_log)
    if staged.returncode != 0:
        commit_a = _git(
            knowledge_root,
            "commit",
            "-m",
            f"chore(retention): provenance snapshot before {policy} truncates "
            f"{len(drop)} entry(ies) from {rel_log} (athenaeum#1418)",
            "--",
            rel_log,
        )
        if commit_a.returncode != 0:
            msg = f"provenance-snapshot commit failed: {commit_a.stderr.strip()}"
            log.error("retention: %s", msg)
            outcome.errors.append(msg)
            return

    head_result = _git(knowledge_root, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        msg = f"could not resolve recovering commit SHA: {head_result.stderr.strip()}"
        log.error("retention: %s", msg)
        outcome.errors.append(msg)
        return
    recovering_sha = head_result.stdout.strip()

    swept_at = now_iso()
    archive_name = f"{log_path.name}.dropped-{swept_at.replace(':', '').replace('.', '')}.jsonl"
    archive_path = destination_root / "retention" / family / archive_name
    archive_rel_for_ledger = _relpath_for_pointer(knowledge_root, archive_path)

    # Ledger write BEFORE any mutation (AC2, fail-closed ordering matching
    # decay_sweep's issue athenaeum#969 AC1 precedent): a page can never be
    # truncated without a durable record of why first.
    ledger_records = [
        RetentionLedgerRecord(
            family=family,
            log_path=rel_log,
            locator=cand.entry.locator,
            byte_length=cand.entry.byte_length,
            content_sha256=hashlib.sha256(cand.entry.raw_line.encode("utf-8")).hexdigest(),
            reason=cand.reason,
            policy=policy,
            swept_at=swept_at,
            recovering_commit=recovering_sha,
            archive_path=archive_rel_for_ledger,
        )
        for cand in drop
    ]
    try:
        write_retention_ledger(ledger_records, cache_dir=cache_dir)
    except Exception as exc:  # noqa: BLE001 -- must abort, never truncate past this
        msg = (
            f"retention-ledger write failed ({type(exc).__name__}): {exc} - "
            "refusing to truncate (issue athenaeum#1418 AC2)"
        )
        log.error("retention: %s", msg)
        outcome.errors.append(msg)
        return

    # Archive the dropped content BEFORE the live rewrite (AC6: never a
    # window where the content exists in neither place).
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_text(
            archive_path, "\n".join(cand.entry.raw_line for cand in drop) + "\n"
        )
    except OSError as exc:
        msg = f"archive write to {archive_path} failed: {exc} - refusing to truncate"
        log.error("retention: %s", msg)
        outcome.errors.append(msg)
        return
    outcome.archive_path = archive_path

    drop_indices = {cand.entry.index for cand in drop}
    kept_lines = [e.raw_line for e in entries if e.index not in drop_indices]
    atomic_write_text(log_path, ("\n".join(kept_lines) + "\n") if kept_lines else "")

    add_paths = [rel_log]
    try:
        archive_rel_in_repo = str(archive_path.resolve().relative_to(knowledge_root.resolve()))
        add_paths.append(archive_rel_in_repo)
    except ValueError:
        pass  # destination is outside knowledge_root (adapter/pii-vault) - nothing to commit there
    commit_b = _git(knowledge_root, "add", "--", *add_paths)
    if commit_b.returncode != 0:
        msg = f"git add failed while committing truncation: {commit_b.stderr.strip()}"
        log.error("retention: %s", msg)
        outcome.errors.append(msg)
        return
    commit_b2 = _git(
        knowledge_root,
        "commit",
        "-m",
        f"chore(retention): {policy} truncated {len(drop)} entry(ies) from "
        f"{rel_log} (athenaeum#1418)",
        "--",
        *add_paths,
    )
    if commit_b2.returncode != 0:
        msg = f"truncation commit failed: {commit_b2.stderr.strip()}"
        log.error("retention: %s", msg)
        outcome.errors.append(msg)
        return

    outcome.dropped = drop
    outcome.applied = True
    outcome.committed = True
    log.info(
        "retention: %s truncated %d entry(ies) from %s; committed (athenaeum#1418)",
        policy,
        len(drop),
        rel_log,
    )


def apply_retention(
    knowledge_root: Path,
    log_path: Path,
    *,
    family: str,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
) -> RetentionOutcome:
    """Enforce *family*'s configured policy against *log_path* (issue
    athenaeum#1418).

    Resolves policy via :func:`athenaeum.config.resolve_retention_policy`:

    * ``None`` (no ``librarian.retention`` block at all) -- a true no-op,
      matching AC1's "empty/absent config yields today's behaviour
      unchanged". Nothing is measured, nothing is mutated.
    * ``"never-truncate"`` -- measures and reports (AC7-style: report, never
      act), records the explicit policy on the outcome, mutates nothing.
    * ``"truncate-top"`` -- :func:`plan_truncate_top` then
      :func:`_apply_drop_set` (AC2/AC6).

    ``"librarian-decides"`` is NOT handled here -- see
    :func:`apply_librarian_decides`, which needs an injected decision
    callable this function has no parameter for.
    """
    policy = resolve_retention_policy(config, family)
    entries = build_entries(log_path)
    total_bytes = sum(e.byte_length for e in entries)
    outcome = RetentionOutcome(
        family=family,
        log_path=log_path,
        policy=policy,
        scanned=len(entries),
        total_bytes=total_bytes,
    )
    if policy is None or policy == "never-truncate":
        return outcome
    if policy == "librarian-decides":
        outcome.errors.append(
            "librarian-decides requires apply_librarian_decides(), not "
            "apply_retention() (issue athenaeum#1418)"
        )
        return outcome

    max_bytes = resolve_retention_max_bytes(config, family)
    outcome.max_bytes = max_bytes
    if total_bytes <= max_bytes:
        return outcome

    wroot = wiki_root if wiki_root is not None else knowledge_root / "wiki"
    log_relpath = _relpath_for_pointer(knowledge_root, log_path)
    cited_locators = find_cited_locators(wroot, log_relpath)
    drop, retained_cited = plan_truncate_top(
        entries, max_bytes=max_bytes, cited_locators=cited_locators
    )
    outcome.retained_cited = retained_cited
    _apply_drop_set(
        knowledge_root,
        log_path,
        entries,
        drop,
        family=family,
        policy=policy,
        config=config,
        cache_dir=cache_dir,
        outcome=outcome,
    )
    if not outcome.applied and drop and not outcome.errors:
        # Defensive -- _apply_drop_set always sets an error on any early
        # return; this branch should be unreachable but must never silently
        # under-report a non-applied truncation.
        outcome.errors.append("truncate-top planned a drop but did not apply it")
    return outcome


@dataclass
class LibrarianDecision:
    """What an injected `librarian-decides` decider returns (issue
    athenaeum#1418 AC4): which entries to purge, why each one, and the
    token/dollar cost of reaching that decision.

    This module never constructs one itself with a non-empty `purge_indices`
    -- see :func:`apply_librarian_decides` for why the actual LLM call is
    deliberately kept out of this module's own dependency graph.
    """

    purge_indices: list[int] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=TokenUsage)
    provider: str = "api"


def apply_librarian_decides(
    knowledge_root: Path,
    log_path: Path,
    *,
    family: str,
    config: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    wiki_root: Path | None = None,
    decide: Callable[[list[RetentionEntry]], LibrarianDecision] | None = None,
) -> RetentionOutcome:
    """Enforce the ``librarian-decides`` policy (issue athenaeum#1418 AC4).

    *decide* is the reasoning-tier integration seam: a pure function from
    the family's current entries to a :class:`LibrarianDecision`. This
    module makes no LLM call itself and threads no ``client``/model
    parameter through its OWN call chain -- exactly the "no LLM calls,
    structurally" contract :mod:`athenaeum.decay_sweep` states for its
    mechanical policies, kept true here too by pushing the one genuinely
    non-deterministic policy behind an injected boundary instead of
    importing :mod:`athenaeum.reasoning_tiers` (a different lane's file)
    directly. A caller that HAS a live reasoning tier supplies *decide*;
    nothing in athenaeum ships one by default.

    ``decide is None`` (no reasoning tier wired up -- true for every caller
    in this repo today, since wiring one is future work) is the
    DETERMINISTIC path AC4 requires stay distinguishable from silence: it
    purges nothing, and a zero-usage spend record is still written -- "a
    deterministic path logs zero rather than nothing" (AC4's own words) --
    via :func:`athenaeum.spend.build_record` (the fleet format), appended to
    THIS module's own retention ledger rather than
    :func:`athenaeum.spend.record_spend`, because that function explicitly
    no-ops a zero-usage call ("Nothing happened — don't clutter the
    ledger", its own docstring) which is precisely the behaviour AC4
    forbids here.

    Purge indices *decide* returns are still subject to AC6's citation
    check, identically to `truncate-top` -- a cited entry is never purged
    regardless of what the decider said.
    """
    policy = resolve_retention_policy(config, family)
    entries = build_entries(log_path)
    total_bytes = sum(e.byte_length for e in entries)
    outcome = RetentionOutcome(
        family=family,
        log_path=log_path,
        policy=policy,
        scanned=len(entries),
        total_bytes=total_bytes,
    )
    if policy != "librarian-decides":
        outcome.errors.append(
            f"family {family!r} policy is {policy!r}, not 'librarian-decides' "
            "(issue athenaeum#1418)"
        )
        return outcome

    decision = decide(entries) if decide is not None else LibrarianDecision()

    wroot = wiki_root if wiki_root is not None else knowledge_root / "wiki"
    log_relpath = _relpath_for_pointer(knowledge_root, log_path)
    cited_locators = find_cited_locators(wroot, log_relpath)
    by_index = {e.index: e for e in entries}
    drop: list[RetentionCandidate] = []
    retained_cited: list[RetentionCandidate] = []
    for idx in decision.purge_indices:
        entry = by_index.get(idx)
        if entry is None:
            outcome.errors.append(f"librarian-decides named unknown entry index {idx}")
            continue
        reason = decision.reasons.get(idx, "librarian-decides: purged, no reason given")
        if entry.locator in cited_locators:
            retained_cited.append(
                RetentionCandidate(
                    entry,
                    reason=f"librarian-decides proposed dropping {entry.locator} "
                    f"({reason}), but it is cited by a live preserved-log: "
                    "reference - retained instead (issue athenaeum#1418 AC6)",
                    cited=True,
                )
            )
            continue
        drop.append(RetentionCandidate(entry, reason=reason))
    outcome.retained_cited = retained_cited

    # AC4: log spend in the fleet format regardless of whether anything was
    # purged -- built via the SAME record shape every other librarian run
    # type uses, written to our own ledger (never spend.record_spend, which
    # would silently drop a zero-usage record).
    outcome.spend_record = build_record(
        decision.usage,
        run_type=LIBRARIAN_DECIDES_RUN_TYPE,
        provider=decision.provider,
    )
    try:
        _append_ledger_line(
            retention_ledger_path(cache_dir),
            json.dumps(
                {
                    "v": RETENTION_LEDGER_SCHEMA_VERSION,
                    "family": family,
                    "spend": outcome.spend_record,
                },
                separators=(",", ":"),
            )
            + "\n",
        )
    except OSError as exc:
        outcome.errors.append(f"spend-record ledger append failed: {exc}")

    _apply_drop_set(
        knowledge_root,
        log_path,
        entries,
        drop,
        family=family,
        policy=policy,
        config=config,
        cache_dir=cache_dir,
        outcome=outcome,
    )
    return outcome
