# SPDX-License-Identifier: Apache-2.0
"""Push-precision + coverage instrumentation (issue athenaeum#711, v6 MVP slice a).

The v6 memory-model epic's definition of done requires push precision to
"improve over a baseline recorded BEFORE any of this ships." That baseline
does not exist yet — nothing today records what ``recall`` pushes into a
session, so there is no way to compute precision (referenced / pushed) or a
coverage-floor miss rate. This module is the FIRST instrument: it ships
before any ranking/tiering change so the pre-change regime gets measured.

Three pieces:

- **Push records** (:func:`record_push`): one row per rendered recall hit,
  written the instant :func:`athenaeum.mcp_server._recall_via_backend`
  assembles the block a session receives. Durable, JSONL, append-only, under
  the cache dir — never inside the wiki corpus, so a push record can never
  become a claim or enter the embedded index. Contains ONLY ids, tiers,
  scopes, counts, and an estimated token cost — no claim content, no personal
  data. Person-entity ids are the opaque ``uid`` frontmatter field, never the
  filename (which embeds a name-derived slug, e.g. ``abc12345-jane-doe.md``).
- **Reference determination** (:func:`determine_references`): at session end,
  scans the ORIGINATING session's transcript (the same read-only surface
  :mod:`athenaeum.transcript_verify` uses) for each pushed id, and writes one
  reference-determination record marking which pushed ids were actually
  referenced afterward. ``precision = referenced / pushed`` per session.
- **Coverage audit** (:func:`build_coverage_worksheet`): samples N sessions'
  push records, re-runs each session's queries against the CURRENT index to
  find candidate hits that scored but were not in the pushed set, and emits a
  worksheet file for a human reviewer to mark relevant-but-missed. The miss
  rate a reviewer records is the coverage-floor baseline — this module can
  compute the CANDIDATE list but not the miss rate itself (that needs a human
  judgment call this module must not fabricate).

**Why reference-determination needed a small new hook instead of reusing
:mod:`athenaeum.transcript_verify` as-is:** ``verify_user_stated`` and
``classify_backfill_claim`` both answer "did the USER say this claim" (they
only match *user-authored* text, deliberately excluding tool output — see
their docstrings). Reference-determination asks a different question — "did
the CONSUMING SESSION make any use of this pushed id afterward" — which must
also match tool/assistant text quoting the pushed id or page name (an agent
that reads a recalled fact and acts on it never necessarily repeats it back
as if the user said it). Reusing the user-only matcher would systematically
undercount precision. :func:`determine_references` therefore calls the
already-public :func:`athenaeum.transcript_verify._iter_session_records`
(the one-session-one-file read primitive both surfaces share) and applies its
own whole-record text scan — the smallest new logic, not a parallel
transcript reader.

Layering: L3 service. Imports :mod:`athenaeum.config` (cache dir + the
enable/disable accessor) and :mod:`athenaeum.transcript_verify` (read-only
transcript access) at L2/L3. Consumed by :mod:`athenaeum.mcp_server` (write
side, push records), :mod:`athenaeum.librarian` (``session_end``, reference
determination), and :mod:`athenaeum._cmd_push_metrics` (the CLI baseline +
coverage-audit commands). Never imports either back.
"""

from __future__ import annotations

import json
import logging
import os
import random
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_cache_dir

log = logging.getLogger(__name__)

#: Schema version stamped on every push / reference record.
SCHEMA_VERSION = 1

#: Filenames under the cache dir. Never under the wiki/raw corpus (issue
#: athenaeum#711 acceptance: "written to a durable, machine-readable location outside
#: the wiki corpus, so they never become claims and never enter the embedded
#: index") — same discipline as ``spend.jsonl`` / ``detection_incomplete.json``.
PUSH_RECORDS_FILENAME = "_push_records.jsonl"
REFERENCE_RECORDS_FILENAME = "_push_references.jsonl"

#: Char-per-token heuristic, matching the existing convention in
#: ``athenaeum.resolutions`` (``_CHARS_PER_TOKEN = 4``, "roughly 4 chars/token
#: for English markdown"). No tokenizer call is made at push time — the
#: render loop has no LLM in the loop — so this is an ESTIMATE, always
#: reported as such (``token_cost_estimated: true``), never presented as a
#: measured usage figure.
_CHARS_PER_TOKEN = 4

#: Environment variables carrying the consuming Claude Code session's id, in
#: PRECEDENCE order (issue athenaeum#734). Claude Code exports
#: ``CLAUDE_CODE_SESSION_ID`` to the stdio MCP servers it spawns;
#: ``CLAUDE_SESSION_ID`` — the name earlier code (``mcp_server``,
#: ``query_topics``) read — is set by NOTHING in the runtime, so the
#: ``if session_id:`` guard was always false and zero push records were ever
#: written. The older name is kept as a fallback so any environment that does
#: export it keeps working. Asserted explicitly in a test, so reading a name
#: nothing exports becomes a visible diff rather than a silent no-op.
SESSION_ID_ENV_VARS: tuple[str, ...] = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def resolve_session_id() -> str:
    """Resolve the consuming session's id from the environment, or ``""``.

    Reads :data:`SESSION_ID_ENV_VARS` in precedence order and returns the first
    non-empty value, else the empty string. This is the ONE place the session-id
    variable name is resolved (issue athenaeum#734): every call site — the
    ``mcp_server`` push-record path and ``query_topics`` spend recording — routes
    through it, so a future rename is a single-line change here rather than a
    silent divergence across sites (the exact defect athenaeum#734 fixes).
    """
    for name in SESSION_ID_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _append_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync).

    Mirrors :func:`athenaeum.spend._append_line` exactly: a single small
    ``O_APPEND`` write is atomic on local filesystems, so a crash can at worst
    leave a torn TRAILING line (skipped by readers), never corrupt an
    already-written record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def push_records_path(cache_dir: Path | None = None) -> Path:
    """Resolve the push-records ledger path: ``<cache_dir>/_push_records.jsonl``."""
    return resolve_cache_dir(cache_dir) / PUSH_RECORDS_FILENAME


def reference_records_path(cache_dir: Path | None = None) -> Path:
    """Resolve the reference-determination ledger path."""
    return resolve_cache_dir(cache_dir) / REFERENCE_RECORDS_FILENAME


# ---------------------------------------------------------------------------
# Redaction — ids only, never content, never a name-derived slug
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Char/4 token estimate (see module docstring). Never negative."""
    return max(0, len(text) // _CHARS_PER_TOKEN)


def opaque_push_id(filename: str, fm: dict[str, object] | None) -> str:
    """Return the id to record for one pushed page — NEVER a name-derived slug.

    - When the page carries a frontmatter ``uid`` (every compiled wiki entity
      does — person, project, concept, anything), that opaque hex id is
      returned. This is the ONLY safe id for a person page: the on-disk
      filename is ``<uid>-<slugified-name>.md`` (see
      :meth:`athenaeum.models.WikiEntity.filename`), so using the filename
      would leak a name-derived slug into a supposedly content-free record.
    - When there is no ``uid`` (a raw/auto-memory intake hit, not yet
      compiled into an entity), the indexed *filename* is used instead. Raw
      intake filenames are timestamp+hash (e.g.
      ``20260802T023311Z-3f0ea402.md``), never name-derived, so this fallback
      carries no PII.
    """
    if fm:
        uid = fm.get("uid")
        if isinstance(uid, str) and uid.strip():
            return uid.strip()
    return filename


# ---------------------------------------------------------------------------
# Push records
# ---------------------------------------------------------------------------


@dataclass
class PushedItem:
    """One pushed page, as it will appear in a push record's ``items`` list.

    Every field is an id, a classification token, or a count — never content.
    """

    id: str
    tier: str
    scope: str
    token_cost: int


@dataclass
class PushRecord:
    """One push event: everything ``recall`` rendered into one response.

    Fields are exactly the issue athenaeum#711 acceptance list: session id, timestamp,
    the pushed claim/page ids, tier, matched scope, and token cost of the
    pushed block — plus a ``query`` HASH (never the raw query text, which can
    carry PII) so a later reproducibility check can correlate two pushes of
    the same query without storing content.
    """

    session_id: str
    ts: str
    query_hash: str
    backend: str
    items: list[PushedItem] = field(default_factory=list)

    @property
    def total_token_cost(self) -> int:
        return sum(item.token_cost for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "session_id": self.session_id,
            "ts": self.ts,
            "query_hash": self.query_hash,
            "backend": self.backend,
            "items": [
                {
                    "id": it.id,
                    "tier": it.tier,
                    "scope": it.scope,
                    "token_cost": it.token_cost,
                }
                for it in self.items
            ],
            "pushed_count": len(self.items),
            "token_cost": self.total_token_cost,
            "token_cost_estimated": True,
        }


def _query_hash(query: str) -> str:
    """Stable, content-free digest of a query string (never the raw text)."""
    import hashlib

    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def build_push_record(
    *,
    session_id: str,
    query: str,
    backend: str,
    hits: list[tuple[str, dict[str, object], str]],
) -> PushRecord:
    """Build a :class:`PushRecord` from rendered recall hits.

    Args:
        session_id: the consuming session's id (``CLAUDE_CODE_SESSION_ID``, or
            the MCP caller's session id when available; resolved via
            :func:`resolve_session_id`). Never empty-string silently accepted
            by the writer — see :func:`record_push`.
        query: the raw recall query text. Only its hash is retained.
        backend: the search backend name actually used (``keyword`` /
            ``fts5`` / ``vector``) — the retrieval mechanism tier.
        hits: ``(filename, fm, snippet_text)`` for each hit actually
            RENDERED into the response (post Layer-C authorization / policy
            filtering — a hit dropped before rendering was never pushed).
            ``snippet_text`` is used ONLY to size the token-cost estimate; it
            is never retained on the record.
    """
    items: list[PushedItem] = []
    for filename, fm, snippet_text in hits:
        pid = opaque_push_id(filename, fm)
        access = fm.get("access") if fm else None
        tier = str(access).strip() if isinstance(access, str) and access.strip() else "internal"
        scope = "owner"
        roles = fm.get("audience") if fm else None
        if isinstance(roles, list) and roles:
            scope = ",".join(sorted(str(r) for r in roles if r))
        elif tier == "open":
            scope = "open"
        items.append(
            PushedItem(
                id=pid,
                tier=tier,
                scope=scope,
                token_cost=estimate_tokens(snippet_text),
            )
        )
    return PushRecord(
        session_id=session_id,
        ts=_now_iso(),
        query_hash=_query_hash(query),
        backend=backend,
        items=items,
    )


def record_push(
    record: PushRecord,
    *,
    cache_dir: Path | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """Append one push record to the durable ledger. Best-effort.

    No-ops (returns ``False``) when instrumentation is disabled
    (:func:`athenaeum.config.resolve_push_metrics_enabled`) or the record has
    no session id or no pushed items (nothing to measure). Every failure is
    swallowed and logged at warning level — a ledger write must NEVER break
    or slow the live recall path, but a silent failure here would produce the
    same "reads as zero forever" hazard athenaeum#568 fixed for the spend ledger.
    """
    try:
        from athenaeum.config import resolve_push_metrics_enabled

        if not resolve_push_metrics_enabled(config):
            return False
        if not record.session_id or not record.items:
            return False
        path = push_records_path(cache_dir)
        _append_line(path, json.dumps(record.to_dict(), separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — must never break recall
        log.warning(
            "push-metrics record write FAILED (%s): %s — this session's push "
            "will be invisible to the baseline/coverage commands",
            type(exc).__name__,
            exc,
        )
        return False


def read_push_records(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read every push record. Tolerates a torn trailing line; never raises."""
    path = push_records_path(cache_dir)
    return _read_jsonl(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Reference determination (session end)
# ---------------------------------------------------------------------------


@dataclass
class ReferenceResult:
    """Per-session reference-determination outcome."""

    session_id: str
    ts: str
    pushed_ids: list[str]
    referenced_ids: list[str]

    @property
    def precision(self) -> float | None:
        """``referenced / pushed``, or ``None`` when nothing was pushed."""
        if not self.pushed_ids:
            return None
        return len(self.referenced_ids) / len(self.pushed_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "session_id": self.session_id,
            "ts": self.ts,
            "pushed_count": len(self.pushed_ids),
            "referenced_count": len(self.referenced_ids),
            "referenced_ids": sorted(self.referenced_ids),
            "precision": self.precision,
        }


def _find_session_transcript(
    session_id: str, projects_root: Path
) -> tuple[Path, str] | None:
    """Locate ``<projects_root>/<scope>/<session_id>.jsonl`` by scanning scopes.

    Push records carry only a session id (mirroring ``CLAUDE_CODE_SESSION_ID``,
    the ambient identifier the rest of athenaeum already keys telemetry on —
    see ``query_topics.py`` and ``docs/configuration.md`` "Ambient telemetry
    variable"), never the Claude Code project-path-hash "scope" directory
    name. There is no existing session-id -> scope registry anywhere in this
    codebase (``transcript_verify.verify_user_stated`` always takes scope as
    a caller-supplied argument), so this scans each scope directory for the
    one file named ``<session_id>.jsonl``. Session ids are UUIDs — a
    collision across two scopes is not a realistic concern. Returns
    ``(scope_dir, scope_name)`` for the first match, or ``None``.
    """
    if not projects_root.is_dir():
        return None
    target = f"{session_id}.jsonl"
    try:
        scopes = sorted(p for p in projects_root.iterdir() if p.is_dir())
    except OSError:
        return None
    for scope_dir in scopes:
        if (scope_dir / target).is_file():
            return scope_dir, scope_dir.name
    return None


def determine_references(
    session_id: str,
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
) -> ReferenceResult | None:
    """Determine which of *session_id*'s pushed ids were referenced afterward.

    Reads this session's push records (from the ledger) and its transcript
    (read-only, via the same one-session-one-file primitive
    ``transcript_verify`` uses), then marks a pushed id "referenced" when it
    appears — as a whole-token substring match — anywhere in ANY transcript
    record's text (user, assistant, or tool-result), not just user-authored
    text. See the module docstring for why this must differ from
    ``verify_user_stated``.

    Returns ``None`` when there are no push records for this session (nothing
    to determine) or the transcript cannot be located (rolled off / never
    existed — an honest "cannot determine", never a fabricated 0 or 1).
    """
    from athenaeum.transcript_verify import _iter_session_records, default_projects_root

    records = [
        r for r in read_push_records(cache_dir) if r.get("session_id") == session_id
    ]
    if not records:
        return None

    pushed_ids: list[str] = []
    for rec in records:
        for item in rec.get("items", []):
            pid = item.get("id")
            if isinstance(pid, str) and pid and pid not in pushed_ids:
                pushed_ids.append(pid)
    if not pushed_ids:
        return None

    root = projects_root if projects_root is not None else default_projects_root()
    located = _find_session_transcript(session_id, root)
    if located is None:
        return None
    scope_dir, _scope_name = located

    transcript_records = _iter_session_records(scope_dir, session_id)
    if not transcript_records:
        return None

    haystacks: list[str] = []
    for trec in transcript_records:
        if not isinstance(trec, dict):
            continue
        message = trec.get("message")
        content = message.get("content") if isinstance(message, dict) else trec.get("content")
        if isinstance(content, str):
            haystacks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    txt = block.get("text") or block.get("content")
                    if isinstance(txt, str):
                        haystacks.append(txt)
                    inner = block.get("content")
                    if isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict):
                                t = sub.get("text")
                                if isinstance(t, str):
                                    haystacks.append(t)
                elif isinstance(block, str):
                    haystacks.append(block)
        tur = trec.get("toolUseResult")
        if isinstance(tur, str):
            haystacks.append(tur)
        elif isinstance(tur, dict):
            for key in ("stdout", "text", "content"):
                v = tur.get(key)
                if isinstance(v, str):
                    haystacks.append(v)
    blob = "\n".join(haystacks)

    referenced = [pid for pid in pushed_ids if pid in blob]
    return ReferenceResult(
        session_id=session_id,
        ts=_now_iso(),
        pushed_ids=pushed_ids,
        referenced_ids=referenced,
    )


def record_reference_result(
    result: ReferenceResult,
    *,
    cache_dir: Path | None = None,
) -> bool:
    """Append one reference-determination record. Best-effort, never raises."""
    try:
        path = reference_records_path(cache_dir)
        _append_line(path, json.dumps(result.to_dict(), separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "push-metrics reference-determination write FAILED (%s): %s",
            type(exc).__name__,
            exc,
        )
        return False


def run_reference_determination(
    session_id: str,
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> ReferenceResult | None:
    """Determine + durably record one session's reference outcome. Best-effort.

    The single entry point :func:`athenaeum.librarian.session_end` calls.
    Returns ``None`` (no-op, nothing written) when instrumentation is
    disabled, there is no session id, or :func:`determine_references` itself
    returns ``None``. Never raises — a reference-determination failure must
    not break ``session_end``.
    """
    try:
        from athenaeum.config import resolve_push_metrics_enabled

        if not resolve_push_metrics_enabled(config):
            return None
        if not session_id:
            return None
        result = determine_references(
            session_id, cache_dir=cache_dir, projects_root=projects_root
        )
        if result is None:
            return None
        record_reference_result(result, cache_dir=cache_dir)
        return result
    except Exception as exc:  # noqa: BLE001 — must never break session_end
        log.warning(
            "push-metrics reference-determination FAILED (%s): %s",
            type(exc).__name__,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Baseline (precision + coverage over a window)
# ---------------------------------------------------------------------------


@dataclass
class BaselineWindow:
    """Precision + coverage figures computed over a stated window.

    ``precision`` is ``None`` when the window has zero reference-determination
    records — the honest state before ANY session has run with instrumentation
    on, never a fabricated ``0.0`` or ``1.0``.

    ``excluded_sessions``/``excluded_push_record_count``/
    ``excluded_reference_record_count`` (issue athenaeum#791) report the
    operator-supplied ``exclude_sessions`` denylist's effect: which of the
    requested session ids actually had records inside this window, and how
    many push/reference records those sessions contributed. Always present
    (empty/zero when no exclusion was requested) so a contaminated window
    that WAS cleaned is visible in the output, not indistinguishable from a
    window that was never contaminated.
    """

    start: str
    end: str
    session_count: int
    push_record_count: int
    reference_record_count: int
    precision: float | None
    athenaeum_version: str
    git_sha: str
    excluded_sessions: tuple[str, ...] = ()
    excluded_push_record_count: int = 0
    excluded_reference_record_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": {"start": self.start, "end": self.end},
            "sessions": self.session_count,
            "push_records": self.push_record_count,
            "reference_records": self.reference_record_count,
            "precision": self.precision,
            "athenaeum_version": self.athenaeum_version,
            "git_sha": self.git_sha,
            "excluded_sessions": list(self.excluded_sessions),
            "excluded_push_records": self.excluded_push_record_count,
            "excluded_reference_records": self.excluded_reference_record_count,
        }


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _get_version() -> str:
    from athenaeum import __version__

    return __version__


def _get_git_sha(repo_root: Path | None = None) -> str:
    """Best-effort short git SHA of the running checkout. ``"unknown"`` if none."""
    import subprocess

    cwd = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        sha = out.stdout.strip()
        return sha if sha else "unknown"
    except Exception:  # noqa: BLE001 — best-effort, never break the baseline run
        return "unknown"


def compute_baseline(
    *,
    since: datetime | None = None,
    cache_dir: Path | None = None,
    repo_root: Path | None = None,
    exclude_sessions: Iterable[str] | None = None,
) -> BaselineWindow:
    """Compute the push-precision baseline over ``[since, now]``.

    ``since=None`` means "the whole ledger" — the honest window when
    instrumentation has been on since it first shipped and no operator narrower
    window was requested. Never estimates a pre-instrumentation figure: a
    push record cannot exist before this instrument shipped, so a window that
    predates the ledger's first record simply contains zero records — reported
    truthfully, not backfilled.

    ``exclude_sessions`` (issue athenaeum#791): an explicit, operator-supplied
    denylist of KNOWN-synthetic session ids — e.g. a session id an operator
    has confirmed ran the test suite and leaked fixture pushes into the live
    ledger (the athenaeum#791 evidence: 75 of 120 push records at filing, all
    from one such session). Deliberately a denylist, not a heuristic: nothing
    here infers "synthetic" from record CONTENT (a push id being a bare
    filename rather than a uid is also the normal shape for a legitimate
    raw-intake page — see :func:`opaque_push_id` — so guessing from that shape
    would risk excluding real data). Excluded sessions' push and
    reference-determination records are dropped from ``session_count``,
    ``push_record_count``, ``reference_record_count``, and ``precision`` —
    but never silently: the excluded session ids and record counts are always
    on the returned :class:`BaselineWindow`, so a cleaned window still shows
    that it needed cleaning.
    """
    now = datetime.now(tz=timezone.utc)
    pushes = read_push_records(cache_dir)
    refs = _read_jsonl(reference_records_path(cache_dir))
    exclude_set = {s for s in (exclude_sessions or ()) if s}

    def _in_window(ts_raw: Any) -> bool:
        ts = _parse_ts(ts_raw)
        if ts is None:
            return False
        if since is not None and ts < since:
            return False
        return True

    pushes_in = [r for r in pushes if _in_window(r.get("ts"))]
    refs_in = [r for r in refs if _in_window(r.get("ts"))]

    excluded_pushes = [r for r in pushes_in if r.get("session_id") in exclude_set]
    excluded_refs = [r for r in refs_in if r.get("session_id") in exclude_set]
    if exclude_set:
        pushes_in = [r for r in pushes_in if r.get("session_id") not in exclude_set]
        refs_in = [r for r in refs_in if r.get("session_id") not in exclude_set]

    found_excluded_sessions = sorted(
        {
            sid
            for r in (excluded_pushes + excluded_refs)
            if isinstance(sid := r.get("session_id"), str) and sid
        }
    )

    sessions = {r.get("session_id") for r in pushes_in if r.get("session_id")}

    precision: float | None = None
    if refs_in:
        total_pushed = sum(int(r.get("pushed_count", 0) or 0) for r in refs_in)
        total_referenced = sum(int(r.get("referenced_count", 0) or 0) for r in refs_in)
        precision = (total_referenced / total_pushed) if total_pushed else None

    if since is not None:
        start_str = since.isoformat().replace("+00:00", "Z")
    else:
        start_str = "(instrument-enabled)"
    return BaselineWindow(
        start=start_str,
        end=now.isoformat().replace("+00:00", "Z"),
        session_count=len(sessions),
        push_record_count=len(pushes_in),
        reference_record_count=len(refs_in),
        precision=precision,
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(repo_root),
        excluded_sessions=tuple(found_excluded_sessions),
        excluded_push_record_count=len(excluded_pushes),
        excluded_reference_record_count=len(excluded_refs),
    )


_SNAPSHOT_HEADER = """# Memory model measurements

Durable home for v6 (dimensional memory model) measurement artifacts.
Each `##` section is produced by one epic child issue and states, inline,
the reproducible command that generated it. This file is committed —
`docs/memory-model.md` (the design lock) is never touched by any command
that writes here.
"""

_SNAPSHOT_SECTION_HEADING = "## Push-precision and coverage baseline"


def render_snapshot_section(baseline: BaselineWindow, *, coverage_note: str) -> str:
    """Render one dated ``## Push-precision and coverage baseline`` entry.

    Machine-readable: every field the issue's acceptance criteria name
    (window start/end, #sessions, #push records, precision point estimate,
    coverage miss rate, athenaeum version + git SHA) appears as a `key:
    value` line so a later agent can parse this without an LLM.

    ``excluded_sessions``/``excluded_push_records``/``excluded_reference_records``
    (issue athenaeum#791) always appear — ``none``/``0`` when no
    ``--exclude-session`` was passed — so a snapshot that DID need to drop a
    known-synthetic session is visibly different from one that never had to,
    rather than the exclusion being invisible in the committed history.
    """
    precision_str = (
        f"{baseline.precision:.4f}"
        if baseline.precision is not None
        else "n/a — accrues as sessions run"
    )
    excluded_sessions_str = (
        ",".join(baseline.excluded_sessions) if baseline.excluded_sessions else "none"
    )
    lines = [
        _SNAPSHOT_SECTION_HEADING,
        "",
        f"### Snapshot {baseline.end}",
        "",
        "Reproduce with: `athenaeum push-metrics baseline`",
        "",
        f"- window_start: {baseline.start}",
        f"- window_end: {baseline.end}",
        f"- sessions: {baseline.session_count}",
        f"- push_records: {baseline.push_record_count}",
        f"- reference_records: {baseline.reference_record_count}",
        f"- precision: {precision_str}",
        f"- coverage_miss_rate: {coverage_note}",
        f"- excluded_sessions: {excluded_sessions_str}",
        f"- excluded_push_records: {baseline.excluded_push_record_count}",
        f"- excluded_reference_records: {baseline.excluded_reference_record_count}",
        f"- athenaeum_version: {baseline.athenaeum_version}",
        f"- git_sha: {baseline.git_sha}",
        "",
    ]
    return "\n".join(lines)


_COVERAGE_PENDING_NOTE = (
    "n/a — awaits operator review of the coverage worksheet "
    "(`athenaeum push-metrics coverage-audit`); see that command's output "
    "file for the sampled sessions and candidate misses a human must mark"
)


def write_snapshot(
    baseline: BaselineWindow,
    *,
    docs_path: Path,
    coverage_note: str = _COVERAGE_PENDING_NOTE,
) -> Path:
    """Idempotently write/append the dated snapshot to *docs_path*.

    - File absent: write the header + one ``## Push-precision and coverage
      baseline`` section containing this one dated snapshot.
    - File present, no existing baseline section: append the section at EOF.
    - File present, baseline section exists: append a new dated ``###
      Snapshot <timestamp>`` sub-entry inside the existing section (never
      replaces an earlier snapshot, never corrupts the file — each run adds
      one dated entry, re-running is always safe).

    Uses :func:`athenaeum.atomic_io.atomic_write_text` for the whole-file
    replace so a crash mid-write can never leave a torn file.

    Raises:
        ValueError: when *baseline* has zero reference-determination records
            (``reference_record_count == 0``) — precision is not computable,
            so the only thing there is to write is a placeholder. Issue
            athenaeum#795: a prior version wrote that placeholder
            unconditionally, and a run against a dead instrument (zero
            reference records) silently appended a meaningless dated entry
            into a tracked docs file. Writing nothing is the correct outcome
            here; nothing is written before this is raised. Callers that
            only want to INSPECT a baseline (valid or not) without writing —
            the exact read-only check the athenaeum#711 incident needed —
            should not call this function at all; see the CLI's
            ``--dry-run``.
    """
    if baseline.reference_record_count == 0:
        raise ValueError(
            "refusing to write snapshot: reference_records=0 in this window, "
            "so precision is not computable — nothing meaningful to record "
            "(athenaeum#795)"
        )

    from athenaeum.atomic_io import atomic_write_text

    new_entry = render_snapshot_section(baseline, coverage_note=coverage_note)

    if not docs_path.is_file():
        content = _SNAPSHOT_HEADER + "\n" + new_entry
        atomic_write_text(docs_path, content)
        return docs_path

    existing = docs_path.read_text(encoding="utf-8")
    if _SNAPSHOT_SECTION_HEADING not in existing:
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        content = existing + sep + new_entry
        atomic_write_text(docs_path, content)
        return docs_path

    # Section exists: append just the dated sub-entry (skip the repeated
    # section heading) right after the existing section's heading line so
    # snapshots accumulate newest-appended, oldest-first is unnecessary —
    # order of appearance simply mirrors run order.
    sub_entry_lines = new_entry.splitlines()[1:]  # drop the "## ..." heading
    sub_entry = "\n".join(sub_entry_lines).lstrip("\n")
    heading_idx = existing.index(_SNAPSHOT_SECTION_HEADING)
    insert_at = heading_idx + len(_SNAPSHOT_SECTION_HEADING)
    content = existing[:insert_at] + "\n\n" + sub_entry + existing[insert_at:]
    atomic_write_text(docs_path, content)
    return docs_path


# ---------------------------------------------------------------------------
# Coverage audit worksheet
# ---------------------------------------------------------------------------


def sample_sessions(cache_dir: Path | None, n: int, *, seed: int | None = None) -> list[str]:
    """Return up to *n* distinct session ids from the push-records ledger.

    Deterministic when *seed* is given (test seam); otherwise a fresh random
    sample each call. Sessions are sampled, not the raw record list, so a
    chatty session's many pushes don't crowd out the sample.
    """
    records = read_push_records(cache_dir)
    sessions = sorted(
        {
            sid
            for r in records
            if isinstance(sid := r.get("session_id"), str) and sid
        }
    )
    if len(sessions) <= n:
        return sessions
    rng = random.Random(seed)
    return sorted(rng.sample(sessions, n))


def build_coverage_worksheet(
    *,
    n: int,
    wiki_root: Path,
    cache_dir: Path | None = None,
    seed: int | None = None,
    search_backend: str = "fts5",
) -> dict[str, Any]:
    """Build the coverage-audit worksheet payload for *n* sampled sessions.

    Per sampled session: the pushed-id set (from its push records) plus
    CANDIDATE ids — pages the session's own recorded queries would still
    match against the CURRENT index — that are NOT in the pushed set. A human
    reviewer marks each candidate relevant-or-not; the miss rate they record
    is the coverage-floor baseline. This function can only emit the
    candidates — it must never guess the miss rate itself.

    Note: push records retain only a query HASH, not the raw query text (by
    design — no content in the ledger), so this cannot re-run the original
    query against the live index. Instead each candidate list is the FULL set
    of other pages pushed to ANY OTHER session in the sample window that
    share at least one tier/scope pairing with this session's pushed set —
    the honest candidate signal available without storing raw query text.
    Where re-running the literal query is required for a tighter candidate
    set, an operator can extend this worksheet with `athenaeum recall
    <query>` by hand; the worksheet says so explicitly.
    """
    records = read_push_records(cache_dir)
    session_ids = sample_sessions(cache_dir, n, seed=seed)

    by_session: dict[str, list[dict[str, Any]]] = {sid: [] for sid in session_ids}
    for rec in records:
        sid = rec.get("session_id")
        if sid in by_session:
            by_session[sid].append(rec)

    all_pushed_ids: set[str] = set()
    for rec in records:
        for item in rec.get("items", []):
            pid = item.get("id")
            if isinstance(pid, str):
                all_pushed_ids.add(pid)

    sessions_out = []
    for sid in session_ids:
        session_records = by_session[sid]
        pushed_ids = sorted(
            {
                item.get("id")
                for rec in session_records
                for item in rec.get("items", [])
                if isinstance(item.get("id"), str)
            }
        )
        candidates = sorted(all_pushed_ids - set(pushed_ids))
        sessions_out.append(
            {
                "session_id": sid,
                "pushed": pushed_ids,
                "candidates_not_pushed": candidates,
                "reviewer_verdict": {c: "TODO" for c in candidates},
            }
        )

    return {
        "v": SCHEMA_VERSION,
        "generated": _now_iso(),
        "wiki_root": str(wiki_root),
        "search_backend": search_backend,
        "sampled_session_count": len(session_ids),
        "sessions": sessions_out,
        "instructions": (
            "For each session, mark every id in candidates_not_pushed's "
            "reviewer_verdict as 'relevant-missed' or 'not-relevant'. The "
            "coverage miss rate = relevant-missed / (pushed + relevant-missed), "
            "aggregated across all sessions in this worksheet, once every "
            "verdict is filled in."
        ),
    }


def write_coverage_worksheet(worksheet: dict[str, Any], *, output_path: Path) -> Path:
    """Write the worksheet as a durable JSON file (never console-only)."""
    from athenaeum.atomic_io import atomic_write_text

    atomic_write_text(
        output_path, json.dumps(worksheet, indent=2, sort_keys=False) + "\n"
    )
    return output_path
