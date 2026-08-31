# SPDX-License-Identifier: Apache-2.0
"""Anchored PII-restore repair tooling (issue athenaeum#1037).

Rebuilds the "put back the token a `[contact redacted -> excluded surface]`
marker replaced, IF that token was never PII" idea from
``scripts/pii-restore.py`` on a method that does not depend on a whole-file
``difflib`` alignment between the corpus's pre-migration state and its
current state. athenaeum#691's 2026-08-20 operator lane instrumented that script
against the live corpus and found it blind to **673 of 844** in-scope
markers: ``difflib.SequenceMatcher.get_opcodes()`` only yields a usable
``replace`` span when the marker's new side is EXACTLY one opcode, and any
edit made to the page since the migration (a librarian reshape, a rename)
fragments that span. 533 of 606 pages scored literally zero.

**Two independent, deterministic methods**, matching the two the issue
scopes (no third method, no classifier change — athenaeum#720's detector
lineage and the athenaeum#689 residual-ledger judgment call are explicitly
out of scope here):

1. **Anchored restore with rename-following** (:func:`build_restore_plan`'s
   default path). For each marker, take a small slice of the text
   immediately before and after it on its own line (the "anchor") and search
   for that anchor in the page's OWN git history — walked via ``git log
   --follow``, so a page that was renamed since the migration is still
   found under its old path(s), which a lookup keyed on the CURRENT path
   (the old script's ``MIGRATION_COMMITS`` constant) cannot do. Matching a
   short anchor against one historical revision at a time, rather than
   diffing the whole file in one pass, is what recovers the classes the
   whole-file alignment fragmented on.
2. **Retro-filename class** (:func:`_resolve_retro_filename`). A corrupted
   footnote citing ``raw/retros/<timestamp>--<issue-list>.md`` resolves
   deterministically: the timestamp prefix is a key into ``raw/retros/``'s
   own git-add history (the ``--follow``-independent
   ``git log --diff-filter=A``), which recovers the true filename — and with
   it the issue-number list the marker ate — with no diff alignment
   involved at all, per athenaeum#691's 2026-08-20T11:23Z demonstration.

**Refusal is the default.** :func:`_classify_or_refuse` is the single choke
point both methods route every candidate token through (dispatching to
:func:`classify` for the anchored method's ambiguous prose tokens, or to
:func:`classify_retro_issue_list` for the retro method's unambiguous
filename-position tokens — see that function's docstring for why the two
need different rules even though both recognize "digit-dash shape" as
safe), and a token neither positively recognizes (date/timestamp,
issue-number/id-fragment list, version string/decimal, ISBN, or a
non-person email shape) is reported as residue and never written —
including person-page markers with a discoverable pre-image. A page whose
OWN ``--follow`` history is entirely younger than the migration (the
"reshaped: inherited already-corrupted text from another page's split"
class athenaeum#691's second 2026-08-20 lane comment names) is also residue:
recovering it needs the SOURCE page's provenance, which is cross-page
tracing this issue does not scope.

**Safety pins, built into the write path, not only asserted by a test:**

- :func:`apply_restore_plan` counts the migrated-address population
  (:func:`athenaeum.pii.iter_contact_records` on the excluded ``pii``
  surface) BEFORE writing any page and AGAIN after, and raises
  :class:`PiiRestoreSafetyError` if the count moved — this tool restores
  prose in ``wiki/``, never touches a contact record, so the count must be
  exactly stable; a drift means something in this code path reached the
  excluded surface, which is refused rather than reported after the fact.
- :func:`apply_restore_plan` refuses (raises :class:`PiiRestoreSafetyError`)
  before writing any page whose resolved path falls under the excluded
  surface root or carries an ``excluded`` path component
  (:func:`_is_excluded_path`) — this tool's whole job is restoring corpus
  prose, and it must never be the thing that writes archival contact data
  back onto a corpus-visible page.
- :func:`_classify_or_refuse` raises on any token :func:`classify` does not
  positively recognize — the same choke point both restoration methods call,
  so there is exactly one place in this module capable of accepting a
  restoration, and it fails closed.

**Layering:** L4 domain/pipeline module, alongside :mod:`athenaeum.
storage_migrate` (whose :data:`~athenaeum.storage_migrate.
INLINE_REDACTION_MARKER` this module reuses rather than redefining) and
:mod:`athenaeum.librarian` (whose :func:`~athenaeum.librarian.reindex` the
CLI layer calls after a successful ``--apply --reindex`, never this module
directly). May import L3 services (:mod:`athenaeum.pii`) and L0/L1
(:mod:`athenaeum.atomic_io`) freely. This module is a **pure planner plus a
narrow, guarded writer** — :func:`build_restore_plan` never touches disk;
:func:`apply_restore_plan` is the only function that writes, and only after
the safety pins above pass. The CLI (:mod:`athenaeum._cmd_pii_restore`,
L5) owns argument parsing, locking, and the reindex trigger.

**Read-only unless ``apply_restore_plan`` is called.** Nothing here reaches
outside the ``knowledge_root``/``wiki_root``/``contacts_root`` paths a
caller supplies — this module has no notion of ``~/knowledge`` and no
default root of its own (issue athenaeum#1037's own out-of-scope list: the live
store is athenaeum#691's job, an ``~operator`` run, not this repo's tests).
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from athenaeum.atomic_io import atomic_write_text
from athenaeum.pii import iter_contact_records
from athenaeum.storage_migrate import INLINE_REDACTION_MARKER

#: The migration marker this module restores tokens behind. Re-exported from
#: :mod:`athenaeum.storage_migrate` (the module that writes it) so this file
#: has no marker text of its own to drift out of sync.
MARKER = INLINE_REDACTION_MARKER

#: How much text either side of the marker forms its "anchor" for the
#: history search. Long enough to be reasonably unique within one page's
#: history; short enough to survive nearby prose edits the librarian made
#: after the migration (the exact thing the whole-file diff could not
#: survive). Tunable, not load-bearing to any AC.
ANCHOR_CONTEXT_CHARS = 24


# --------------------------------------------------------------------------- #
# Classification -- the entire safe/unsafe boundary.
#
# Deliberately NOT imported from ``scripts/pii-restore.py``: that file lives
# outside ``src/athenaeum`` and is not shipped in the installed package, so a
# library module the installed ``athenaeum pii-restore`` CLI depends on
# cannot reach it at runtime. The boundary below is the SAME one athenaeum#691's
# 2026-08-20 lane instrumentation validated against the live corpus (restore
# only a positively-recognized non-PII shape; every email stays redacted
# unless it is a service id / role / test-account / known host alias) --
# mirrored intentionally, not re-derived.
# --------------------------------------------------------------------------- #

_ISO_DATE = re.compile(r"^[(\[]?\d{4}-\d{2}-\d{2}\)?$")
_YEAR_RANGE = re.compile(r"^[(\[]?(?:19|20)\d{2}-(?:19|20)\d{2}\)?$")
_DATE_ISH = re.compile(r"(?:19|20)\d{2}-\d{2}(?:-\d{2})?")
_ID_FRAGMENT = re.compile(r"^[(\[]?\d{6,9}\)?$")
_NUM_LIST = re.compile(r"^[\d\s().-]*\d[-\d\s().]*$")
_ISBN = re.compile(r"^\d{13}$")
_DECIMAL = re.compile(r"^\d*\.\d+$")

#: Generic, non-identifying entries safe to ship in a public repo. An
#: operator-specific address does NOT belong here -- see
#: ``scripts/pii-restore.py``'s ``safe_email_exact`` for the live-config
#: extension point that class of value uses; this module's classify() only
#: needs the structural (service-id/role/test-account) buckets, which never
#: hardcode a specific address.
_SAFE_EMAIL_EXACT = frozenset({"git@github.com", "root@example.com"})
_SAFE_EMAIL_SUBSTR = (
    "group.calendar.google.com",
    "iam.gserviceaccount.com",
    "x-access-token",
)
_SAFE_EMAIL_PREFIX = ("noreply@", "no-reply@", "donotreply@", "admin@", "support@", "info@")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def classify(token: str) -> str | None:
    """Return a restore-class name for *token*, or ``None`` to leave it redacted.

    ``None`` is the SAFE direction: an email that is not positively
    recognized as a service id / role address / test account / known host
    alias is a real person's address and stays migrated, full stop -- this
    function never restores the person-contact axes (email/phone), which is
    athenaeum#1037's PII-safety AC. Every caller in this module routes through
    :func:`_classify_or_refuse` rather than this function directly, so a
    ``None`` here becomes a hard refusal, not a silent skip.
    """
    t = token.strip()
    if not t:
        return None
    low = t.lower()

    if _EMAIL_RE.search(t):
        addr_m = _EMAIL_RE.search(t)
        addr = addr_m.group(0).lower() if addr_m else low
        if addr in _SAFE_EMAIL_EXACT or low in _SAFE_EMAIL_EXACT:
            return "email:host-alias/path"
        if any(s in low for s in _SAFE_EMAIL_SUBSTR):
            return "email:service-id"
        if any(addr.startswith(p) for p in _SAFE_EMAIL_PREFIX):
            return "email:role"
        if "test" in addr.split("@")[0]:
            return "email:test-account"
        return None  # a real person address -- stays migrated, always.

    if _ISO_DATE.match(t):
        return "date:iso"
    if _YEAR_RANGE.match(t):
        return "date:year-range"
    if _ISBN.match(t.replace("-", "")):
        return "isbn"
    if _DECIMAL.match(t):
        return "decimal"
    if _ID_FRAGMENT.match(t):
        return "id-fragment"
    if _DATE_ISH.search(t) and not re.search(r"[A-Za-z]{3,}", t):
        return "date:embedded"
    if "--" in t and _NUM_LIST.match(t):
        return "number-list"
    if _NUM_LIST.match(t):
        digits = re.sub(r"\D", "", t)
        # A separator-bearing 10-15 digit run is a plausible real phone
        # number: keep it redacted. This is the phone axis's safety pin --
        # never restore anything that could be a person's phone number.
        if 10 <= len(digits) <= 15 and re.search(r"[-.\s()]", t):
            return None
        return "number-other"
    return None


#: An issue-number list has the same digit-dash SHAPE as a phone number
#: (``801-835-841-843`` vs. ``555-123-4567``), which is exactly why
#: :func:`classify` refuses a bare digit-dash token in that length range --
#: it cannot tell them apart from the token alone. The retro-filename
#: method never has that ambiguity: the value it classifies is not
#: free-standing prose next to a marker, it is the ``--<issue-list>.md``
#: tail of a filename recovered by an EXACT git-history lookup keyed on the
#: timestamp prefix (:func:`_resolve_retro_filename`) -- a position a phone
#: number cannot structurally occupy. So this method gets its own narrow
#: classifier instead of routing through :func:`classify`'s prose-ambiguity
#: heuristic, and :func:`_classify_or_refuse` dispatches to it by *method*.
_ISSUE_LIST_SHAPE = re.compile(r"^\d+(?:-\d+)*$")


def classify_retro_issue_list(value: str) -> str | None:
    """Classify a retro-filename's recovered ``<issue-list>`` tail.

    Returns ``"issue-list"`` for a plain digit-dash run (one or more GitHub
    issue numbers), or ``None`` -- refusing, the safe direction -- for
    anything else a resolved filename tail could theoretically contain.
    """
    if _ISSUE_LIST_SHAPE.match(value.strip()):
        return "issue-list"
    return None


class PiiRestoreSafetyError(RuntimeError):
    """A safety invariant this tool exists to protect would be violated.

    Two of this module's three raise sites fire strictly BEFORE any write --
    an unrecognized token reaching the write path
    (:func:`_classify_or_refuse`, checked per-restoration before its page is
    touched) and a write target under the excluded surface
    (:func:`_is_excluded_path`, checked before that page's write). The
    third, :func:`assert_excluded_population_unchanged`, is a post-hoc
    tripwire: this tool only ever writes to ``wiki/`` pages, so the
    contacts-surface population should be structurally unable to move, but
    the check runs after those (contacts-surface-adjacent-safe-by-
    construction) writes rather than before them, and it is what turns a
    should-never-happen drift into a hard failure instead of a silent
    success report -- not a rollback of the wiki writes already made.
    """


class GitHistoryUnavailableError(RuntimeError):
    """``git log`` itself failed for a page -- history was never consulted.

    Raised by :func:`_history_with_paths` on any non-zero ``git`` exit: no
    repository at the given root, a corrupted repository, a detached/
    unreadable ``HEAD``, or any other git-level error. This is deliberately
    a DIFFERENT signal than an empty-but-successful history (``git`` exits
    0 with no commits for a path that is genuinely new) -- only the latter
    is a real "this page has no pre-image" corpus fact.

    Before athenaeum#1228, :func:`_history_with_paths` returned ``[]`` for
    both cases, so :func:`_plan_anchored_restore` forced every marker into
    ``no-pre-image:page-created-after-migration`` whenever git could not
    even be consulted -- producing a plausible-looking but entirely false
    ``TOTAL RESTORABLE = 0`` (demonstrated against a lane container's
    ``/knowledge`` mount, which carries no ``.git``).
    """


#: Residue reason for a marker :func:`_plan_anchored_restore` could not
#: classify because :func:`_history_with_paths` raised
#: :class:`GitHistoryUnavailableError` -- named so a reader cannot mistake
#: it for ``no-pre-image:page-created-after-migration`` (a statement ABOUT
#: the corpus): this one is a statement about the tool's own environment.
GIT_HISTORY_UNAVAILABLE_REASON = "git-history-unavailable"


def _classify_or_refuse(token: str, *, method: str) -> str:
    """Classify *token* for restoration under *method*, or raise.

    The single choke point every restoration in this module passes through
    -- both :func:`_plan_anchored_restore`/:func:`_plan_retro_filename` at
    plan time AND :func:`apply_restore_plan`'s independent re-check at write
    time. Keeping ALL "is this safe to write" logic behind one function --
    rather than letting each caller re-check a classifier's result inline --
    is what makes "an over-restore attempt on a fixture asserts refusal"
    (issue athenaeum#1037's PII-safety AC) a property of ONE function instead of
    an invariant every call site has to remember to uphold.

    *method* selects WHICH classifier decides: ``"retro-filename-lookup"``
    routes to :func:`classify_retro_issue_list` (a deterministic filename
    position, never ambiguous with a phone number); every other method
    routes to :func:`classify` (the general, deliberately-conservative
    prose-token boundary, which refuses a phone-shaped digit run precisely
    because it CANNOT rule out a real phone number from the token alone).
    """
    restore_class = (
        classify_retro_issue_list(token) if method == "retro-filename-lookup" else classify(token)
    )
    if restore_class is None:
        raise PiiRestoreSafetyError(
            f"refusing to restore {token!r} (method={method!r}): not a "
            "positively-recognized non-PII class -- stays redacted."
        )
    return restore_class


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _show(repo_root: Path, rev: str, relpath: str) -> str | None:
    """``git show <rev>:<relpath>``, or ``None`` if that blob does not exist."""
    proc = _git(repo_root, "show", f"{rev}:{relpath}")
    return proc.stdout if proc.returncode == 0 else None


def _history_with_paths(repo_root: Path, current_relpath: str) -> list[tuple[str, str]]:
    """Every commit touching *current_relpath*, newest-first, WITH the path
    the file had AT that commit -- the rename-following primitive.

    ``git log --follow`` already resolves the rename chain against the given
    starting path; what it does not hand back directly is "which path do I
    pass to ``git show <sha>:<path>`` for commit N" once a rename has
    happened further back. This walks the ``--name-status`` output newest-
    to-oldest, tracking the currently-followed path and updating it the
    moment an ``R<score>\told\tnew`` line's *new* side matches: every commit
    at or before the rename is then paired with the OLD path, which is
    exactly what a raw ``git show <old-sha>:<current-path>`` lookup (the
    thing the pre-athenaeum#1037 script effectively did) gets wrong.

    Raises:
        GitHistoryUnavailableError: if the ``git log`` invocation itself
            failed (non-zero exit) -- see that class's docstring for why
            this must NOT collapse into the empty-list return below (that
            return is reserved for a git command that SUCCEEDED and found
            no history, which is a real, reportable corpus fact).
    """
    proc = _git(
        repo_root,
        "log",
        "--follow",
        "--format=@@%H",
        "--name-status",
        "--",
        current_relpath,
    )
    if proc.returncode != 0:
        raise GitHistoryUnavailableError(
            f"git log --follow failed for {current_relpath!r} under "
            f"{repo_root} (exit {proc.returncode}): "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )
    if not proc.stdout.strip():
        return []
    entries: list[tuple[str, str]] = []
    tracked = current_relpath
    for block in proc.stdout.split("@@")[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        sha = lines[0].strip()
        path_here = tracked
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split("\t")
            status = parts[0]
            if status.startswith("R") and len(parts) == 3:
                old, new = parts[1], parts[2]
                if new == tracked:
                    path_here = new
                    tracked = old
            elif len(parts) == 2 and parts[1] == tracked:
                path_here = parts[1]
        entries.append((sha, path_here))
    return entries


def _find_preimage_token(
    repo_root: Path, current_relpath: str, anchor_before: str, anchor_after: str
) -> tuple[str, str] | None:
    """Search *current_relpath*'s OWN ``--follow`` history for the token the
    marker replaced. Returns ``(token, commit_sha)`` for the nearest
    (most-recent-first) revision whose text matches the anchor, or ``None``.

    Revisions that themselves still carry the marker inside the captured
    span (the current, already-corrupted text, or an intermediate revision
    that inherited it) are skipped rather than accepted -- the search keeps
    walking further back until it finds a revision that actually held real
    content at that position.
    """
    before_pat = re.escape(anchor_before) if anchor_before else r"^"
    after_pat = re.escape(anchor_after) if anchor_after else r"$"
    pattern = re.compile(before_pat + r"(.+?)" + after_pat, re.MULTILINE)
    for sha, path in _history_with_paths(repo_root, current_relpath):
        text = _show(repo_root, sha, path)
        if text is None:
            continue
        m = pattern.search(text)
        if not m:
            continue
        token = m.group(1)
        if not token.strip() or MARKER in token:
            continue
        return token, sha
    return None


# --------------------------------------------------------------------------- #
# Retro-filename class
# --------------------------------------------------------------------------- #

#: A corrupted retro-filename citation: ``retros/<timestamp>--...MARKER....md``.
#: The timestamp is the recoverable key. MARKER need not span the WHOLE
#: ``--``...``.md`` region -- most live citations keep surrounding filename
#: text (an issue-number prefix, a slug suffix, or both) around the marker,
#: e.g. ``retros/<ts>--athenaeum-1091-MARKER-236config.md``. The optional
#: filler on either side is bounded to non-whitespace, non-``/`` characters
#: (never crossing a path separator or a token boundary) so a `.search()`
#: over a line with two DIFFERENT retro citations still anchors each match
#: to its own citation instead of spanning across both.
_RETRO_FRAGMENT_RE = re.compile(
    r"retros/(?P<ts>\d{8}T\d{6}Z)--[^\s/]*?" + re.escape(MARKER) + r"[^\s/]*?\.md"
)


def _resolve_retro_filename(repo_root: Path, timestamp_key: str) -> str | None:
    """Look up ``raw/retros/<timestamp_key>--*.md`` in git-add history.

    Independent of any per-page diff: the timestamp prefix is a
    deterministic key into ``raw/retros/``'s own history, recoverable even
    when the raw file has since rotated out of the working tree (its blob
    is still reachable through ``--all``). Returns the true filename (with
    the issue-number list intact) or ``None`` if no such add is found.
    """
    proc = _git(
        repo_root,
        "log",
        "--all",
        "--diff-filter=A",
        "--name-only",
        "--format=",
        "--",
        f"raw/retros/{timestamp_key}--*.md",
    )
    if proc.returncode != 0:
        return None
    prefix = f"raw/retros/{timestamp_key}--"
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith(prefix) and line.endswith(".md"):
            return Path(line).name
    return None


# --------------------------------------------------------------------------- #
# Marker discovery
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MarkerHit:
    """One occurrence of :data:`MARKER` in the live corpus."""

    #: Page path, relative to the repo root passed to :func:`build_restore_plan`.
    page_relpath: str
    line_no: int  # 1-indexed
    char_offset: int  # 0-indexed offset of MARKER's start within the line
    line_text: str


def _is_excluded_path(path: Path, contacts_root: Path) -> bool:
    """True if *path* is under the excluded surface -- never a legal write target.

    Belt-and-suspenders: a normal ``storage.mapping`` wiring already keeps
    ``contacts_root`` outside ``wiki_root`` entirely, so this should never
    trip in ordinary operation. It exists so athenaeum#1037's "the tool never
    writes under ``excluded/``" AC is a real refusal in the code path, not
    just an accident of directory layout.
    """
    resolved = path.resolve()
    try:
        resolved.relative_to(contacts_root.resolve())
        return True
    except ValueError:
        pass
    return "excluded" in resolved.parts


def find_markers(wiki_root: Path, contacts_root: Path) -> list[MarkerHit]:
    """Every :data:`MARKER` occurrence under *wiki_root*, never under *contacts_root*.

    Sorted by (page, line, offset) for deterministic output. A page is
    skipped, not silently included-and-refused-later, if it resolves under
    the excluded surface -- this function is the single marker-discovery
    entry point both the dry-run and the apply path use, so an excluded page
    is never even a candidate.
    """
    hits: list[MarkerHit] = []
    if not wiki_root.is_dir():
        return hits
    for page in sorted(wiki_root.rglob("*.md")):
        if _is_excluded_path(page, contacts_root):
            continue
        text = page.read_text(encoding="utf-8", errors="replace")
        if MARKER not in text:
            continue
        rel = str(page.relative_to(wiki_root.parent))
        for line_no, line in enumerate(text.split("\n"), start=1):
            for m in re.finditer(re.escape(MARKER), line):
                hits.append(MarkerHit(rel, line_no, m.start(), line))
    return hits


# --------------------------------------------------------------------------- #
# Restore plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Restoration:
    """One marker this tool will (or did) put real text back behind."""

    hit: MarkerHit
    token: str
    restore_class: str
    #: ``"anchored-rename-follow"`` or ``"retro-filename-lookup"``.
    method: str
    #: Commit SHA (anchored method) or resolved retro filename (retro method)
    #: the restoration was recovered from -- provenance for the report.
    source_ref: str


@dataclass(frozen=True)
class ResidueEntry:
    """One marker this tool will NOT restore, with an honest, named reason.

    Never silently dropped: every marker :func:`find_markers` finds ends up
    in either :attr:`RestorePlan.restorations` or
    :attr:`RestorePlan.residue` -- there is no third, unreported bucket.
    """

    hit: MarkerHit
    #: One of: ``"kept:real-pii"``, ``"no-pre-image:page-created-after-migration"``,
    #: ``"no-pre-image:context-not-found"``, ``"retro-filename:not-found-in-history"``,
    #: ``"git-history-unavailable"`` (:data:`GIT_HISTORY_UNAVAILABLE_REASON` --
    #: git itself could not be consulted; NOT a statement about the corpus).
    reason: str


@dataclass
class RestorePlan:
    """The result of scanning a corpus for restorable markers. Never writes."""

    pages_scanned: int
    restorations: list[Restoration] = field(default_factory=list)
    residue: list[ResidueEntry] = field(default_factory=list)

    def counts_by_method_and_class(self) -> dict[str, Counter]:
        """``{method: Counter({restore_class: n})}`` for the dry-run report."""
        out: dict[str, Counter] = {}
        for r in self.restorations:
            out.setdefault(r.method, Counter())[r.restore_class] += 1
        return out

    def residue_counts_by_reason(self) -> Counter:
        return Counter(entry.reason for entry in self.residue)

    def git_history_unavailable_count(self) -> int:
        """How many markers hit :data:`GIT_HISTORY_UNAVAILABLE_REASON`.

        Non-zero here means the classifier could not consult git history for
        at least one marker -- callers (the CLI's dry-run/apply paths) must
        treat that as "cannot report a plan", never fold it into a quiet
        ``TOTAL RESTORABLE`` count (athenaeum#1228).
        """
        return sum(1 for entry in self.residue if entry.reason == GIT_HISTORY_UNAVAILABLE_REASON)


def build_restore_plan(
    repo_root: Path,
    wiki_root: Path,
    contacts_root: Path,
    *,
    limit: int | None = None,
) -> RestorePlan:
    """Scan *wiki_root* for every marker and classify each as restorable or residue.

    Pure -- never writes. *repo_root* is the git repository root the page
    history is walked in (the ``wiki_root``'s parent in the normal
    ``knowledge_root/wiki`` layout, but kept as an explicit parameter so a
    test fixture's repo root and wiki subdirectory can be named separately).
    """
    hits = find_markers(wiki_root, contacts_root)
    if limit is not None:
        hits = hits[:limit]
    pages_scanned = len({h.page_relpath for h in hits})

    plan = RestorePlan(pages_scanned=pages_scanned)
    for hit in hits:
        retro_match = _RETRO_FRAGMENT_RE.search(hit.line_text)
        if retro_match:
            _plan_retro_filename(repo_root, hit, retro_match.group("ts"), plan)
            continue
        _plan_anchored_restore(repo_root, hit, plan)
    return plan


def _plan_retro_filename(
    repo_root: Path, hit: MarkerHit, timestamp_key: str, plan: RestorePlan
) -> None:
    filename = _resolve_retro_filename(repo_root, timestamp_key)
    if filename is None:
        plan.residue.append(ResidueEntry(hit, "retro-filename:not-found-in-history"))
        return
    # filename is "<timestamp>--<issue-list>.md"; the marker stands in for
    # the "<issue-list>" segment exactly.
    issue_list = filename[len(f"{timestamp_key}--") : -len(".md")]
    try:
        restore_class = _classify_or_refuse(issue_list, method="retro-filename-lookup")
    except PiiRestoreSafetyError:
        # Defense in depth: a retro filename's tail should always classify
        # as a safe digit-dash shape. If a resolved filename ever tails off
        # into something else, this is real residue, not a restoration --
        # refusal is still the default.
        plan.residue.append(ResidueEntry(hit, "kept:real-pii"))
        return
    plan.restorations.append(
        Restoration(hit, issue_list, restore_class, "retro-filename-lookup", filename)
    )


def _plan_anchored_restore(repo_root: Path, hit: MarkerHit, plan: RestorePlan) -> None:
    line = hit.line_text
    start = hit.char_offset
    end = start + len(MARKER)
    anchor_before = line[max(0, start - ANCHOR_CONTEXT_CHARS) : start]
    anchor_after = line[end : end + ANCHOR_CONTEXT_CHARS]

    try:
        history = _history_with_paths(repo_root, hit.page_relpath)
        found = _find_preimage_token(repo_root, hit.page_relpath, anchor_before, anchor_after)
    except GitHistoryUnavailableError:
        # git itself could not be consulted for this page -- this is NOT
        # the same as a genuinely empty history, so it must never land in
        # no-pre-image:page-created-after-migration (athenaeum#1228).
        plan.residue.append(ResidueEntry(hit, GIT_HISTORY_UNAVAILABLE_REASON))
        return
    if found is None:
        if len(history) <= 1:
            # The page's own history has nothing before the current
            # (marker-carrying) commit -- it was created after the
            # corruption existed, e.g. a librarian reshape that copied
            # already-corrupted prose into a brand-new page. Recovering
            # this needs the SOURCE page's provenance, which is cross-page
            # tracing this tool does not do (issue athenaeum#1037 scopes exactly
            # two methods, neither of which is this).
            plan.residue.append(
                ResidueEntry(hit, "no-pre-image:page-created-after-migration")
            )
        else:
            plan.residue.append(ResidueEntry(hit, "no-pre-image:context-not-found"))
        return

    token, sha = found
    try:
        restore_class = _classify_or_refuse(token, method="anchored-rename-follow")
    except PiiRestoreSafetyError:
        plan.residue.append(ResidueEntry(hit, "kept:real-pii"))
        return
    plan.restorations.append(
        Restoration(hit, token, restore_class, "anchored-rename-follow", sha)
    )


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ApplyResult:
    pages_changed: int
    sites_restored: int
    pre_count: int
    post_count: int


def assert_excluded_population_unchanged(before: int, after: int) -> None:
    """Refuse if the migrated-address population moved across an apply.

    This tool restores prose in ``wiki/``; it never creates, edits, or
    deletes a contact record on the excluded surface. So this count must be
    EXACTLY stable across every apply -- a drift in either direction means
    something in this code path reached the excluded surface, which is a
    PII-safety regression regardless of direction (over-restoring pulls a
    record back into corpus-adjacent reach; under-counting could just as
    easily mask a lost record). Raises rather than returning a verdict, so a
    caller cannot accidentally ignore it.
    """
    if before != after:
        raise PiiRestoreSafetyError(
            "migrated-address population changed during restore "
            f"({before} -> {after}); refusing -- this tool must never move "
            "a record onto or off the excluded surface."
        )


def apply_restore_plan(
    plan: RestorePlan, *, wiki_root: Path, contacts_root: Path
) -> ApplyResult:
    """Write every restoration in *plan* to disk. The only function in this
    module that touches disk.

    Order of operations is deliberate: count the migrated-address
    population BEFORE any write, write every page, then count again and
    refuse (:func:`assert_excluded_population_unchanged`) if it moved.
    Every restoration's token is re-verified through
    :func:`_classify_or_refuse` here -- a second, independent check at the
    write site, not a re-trust of whatever :func:`build_restore_plan` already
    decided -- so a plan that has been tampered with between build and apply
    (or a caller that constructs a :class:`Restoration` by hand, as the
    over-restore-refusal test does) cannot reach a write with an unsafe
    token.
    """
    pre_count = len(iter_contact_records(contacts_root))

    by_page: dict[str, list[Restoration]] = {}
    for r in plan.restorations:
        _classify_or_refuse(r.token, method=r.method)  # re-verify; see docstring.
        by_page.setdefault(r.hit.page_relpath, []).append(r)

    pages_changed = 0
    repo_root = wiki_root.parent
    for relpath, restorations in by_page.items():
        page_path = repo_root / relpath
        if _is_excluded_path(page_path, contacts_root):
            raise PiiRestoreSafetyError(
                f"refusing to write under the excluded surface: {page_path}"
            )
        new_text = _apply_page_restorations(page_path, restorations)
        atomic_write_text(page_path, new_text)
        pages_changed += 1

    post_count = len(iter_contact_records(contacts_root))
    assert_excluded_population_unchanged(pre_count, post_count)

    return ApplyResult(
        pages_changed=pages_changed,
        sites_restored=len(plan.restorations),
        pre_count=pre_count,
        post_count=post_count,
    )


def _apply_page_restorations(page_path: Path, restorations: list[Restoration]) -> str:
    """Rebuild one page's text with every restoration's marker replaced.

    Applied by (line, offset) rather than a whole-text ``str.replace`` so
    two markers on the same line -- or a token that happens to contain
    marker-like text -- can never cross-contaminate each other's
    substitution.
    """
    text = page_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    by_line: dict[int, list[Restoration]] = {}
    for r in restorations:
        by_line.setdefault(r.hit.line_no, []).append(r)
    for line_no, entries in by_line.items():
        idx = line_no - 1
        line = lines[idx]
        for r in sorted(entries, key=lambda r: r.hit.char_offset, reverse=True):
            offset = r.hit.char_offset
            line = line[:offset] + r.token + line[offset + len(MARKER) :]
        lines[idx] = line
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #


def render_report(plan: RestorePlan, *, applied: bool, result: ApplyResult | None = None) -> str:
    """Human-readable dry-run/apply report: per-class counts by method, and
    every residue reason named -- never a silent "N left redacted".
    """
    mode = "APPLIED" if applied else "DRY RUN"
    lines = [f"=== pii-restore ({mode}) ==="]
    lines.append(f"  pages_scanned: {plan.pages_scanned}")
    total_sites = len(plan.restorations) + len(plan.residue)
    lines.append(f"  marker_sites:  {total_sites}")

    lines.append("")
    lines.append("  restorable by method:" if not applied else "  restored by method:")
    by_method = plan.counts_by_method_and_class()
    total_restored = 0
    for method in sorted(by_method):
        lines.append(f"    {method}:")
        for cls, n in sorted(by_method[method].items()):
            total_restored += n
            lines.append(f"      {cls:24} {n:5}")
    verb = "TOTAL RESTORED" if applied else "TOTAL RESTORABLE"
    lines.append(f"    {verb:26} {total_restored:5}")

    lines.append("")
    lines.append("  residue (never written) by reason:")
    residue_counts = plan.residue_counts_by_reason()
    for reason, n in sorted(residue_counts.items()):
        lines.append(f"    {reason:42} {n:5}")
    lines.append(f"    {'TOTAL RESIDUE':42} {len(plan.residue):5}")

    if applied and result is not None:
        lines.append("")
        lines.append(f"  pages_changed: {result.pages_changed}")
        lines.append(
            f"  migrated-address population: {result.pre_count} -> {result.post_count} "
            "(pinned, unchanged)"
        )

    return "\n".join(lines)
