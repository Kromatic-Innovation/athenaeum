# SPDX-License-Identifier: Apache-2.0
"""``athenaeum context`` — the agent-neutral sidecar core (issue athenaeum#1358).

One process that takes a prompt plus a session id and returns ranked
candidates plus rendered text, replacing the 5+ subprocess fan-out per turn
the shell hooks (``examples/claude-code/user-prompt-recall.sh``) used to run:
``athenaeum query-topics``, one FTS5 ``sqlite3`` query, up to three more
``sqlite3`` description lookups, an optional vector leg, and a ``jq``
envelope build.

**Import-weight contract (the load-bearing constraint of this module):**
this module, and everything it imports AT MODULE SCOPE, must never pull in
``anthropic``, ``chromadb``, or :mod:`athenaeum.librarian`. That is what lets
``import athenaeum.context`` (and the package root it necessarily runs
first, athenaeum#1360) stay under the FTS5-path wall-clock budget this
module's own guard test pins. LLM term extraction is real functionality
here, not out of scope — but it is reached through
:func:`athenaeum.query_topics.extract_topics`, which is ITSELF import-light
(it defers ``athenaeum.provider`` — the module that actually names
``anthropic`` — to inside the function body, same discipline this file
follows for its own optional legs). Do not import ``athenaeum.provider``,
``athenaeum.tiers``, ``athenaeum.batch``, or ``athenaeum.librarian`` at this
module's top level, directly or transitively, for any reason.

**Host-neutral, by design:** this module knows nothing about any host's
wrapper keys for its per-turn hook payload — Claude Code's included. It
returns one plain dict (:func:`build_context`'s return value, "the
envelope" — schema owned and versioned by athenaeum#1359, not here).
Wrapping that envelope for a specific host is an ADAPTER's job
(``examples/claude-code/user-prompt-recall.sh`` today; the athenaeum#1361
cutover script tomorrow) — never this module's. (Deliberately not named
literally here: a `grep` of this file for either of the two forbidden
wrapper-key strings must return nothing — see this issue's own acceptance
criteria and ``tests/test_context_core.py``'s literal-grep guard.)

**Selection and ranking are by relevance alone.** ``memory_tier`` is carried
on each candidate as METADATA ONLY (issue athenaeum#1345 owns this
invariant) — it never appears in a ``WHERE``/``ORDER BY`` clause and never
adjusts a score. Swapping two candidates' ``memory_tier`` values must never
change which candidates are selected or their order.

Layering: L3 service, same tier as :mod:`athenaeum.search` and
:mod:`athenaeum.push_metrics`, which this module deliberately does not
import at module scope (see the import-weight contract above).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.context_schema import SCHEMA_VERSION

log = logging.getLogger(__name__)

# Single-sourced from athenaeum.context_schema (issue athenaeum#1359 review
# finding, Seer/Sentry HIGH): this module's envelope version and the
# schema module's SCHEMA_VERSION were previously two independent literals
# both hand-set to 1, with nothing keeping them equal — a future schema
# bump in one file without the matching edit in the other would silently
# desync, and every envelope this module builds would then fail
# `context_schema.validate_envelope()`'s version check.
#
# This import adds NO marginal cost against this module's import-weight
# contract, but not for the reason `_recall_disabled`'s docstring might
# suggest at a glance: `context.py` IS `athenaeum.context`, a submodule, so
# simply reaching this line has ALREADY forced Python to run
# `athenaeum/__init__.py` (a submodule import always initializes its parent
# package first) — that cost is paid unconditionally by anyone who does
# `import athenaeum.context`, before this module's own body even starts.
# `context_schema.py` then adds nothing further: it has zero athenaeum
# imports of its own (stdlib-only, see its module docstring), so loading it
# is just reading one more small file, not a second trip through the
# package root. Contrast `_recall_disabled` below, whose avoided
# `athenaeum.killswitch` import would have added REAL marginal cost —
# killswitch.py's own chain (`athenaeum.atomic_io`, `athenaeum.config` ->
# `yaml`) — on top of the already-paid package-root cost, on every single
# call.
ENVELOPE_VERSION = SCHEMA_VERSION

DEFAULT_BUDGET_TOKENS = 1200

# Baked-in minimal fallback, used ONLY when the caller supplies no stopword
# set at all (e.g. a fresh cache dir with no cached list yet). Callers that
# have `athenaeum.search.STOPWORDS` available (the canonical list, issue
# athenaeum#46) should pass it in via `stopwords=`; this module does not
# import `athenaeum.search` itself to stay import-minimal — `search.py`
# pulls in `athenaeum.store`/`athenaeum.pii`/`athenaeum.authority`, none of
# which are needed for the read-only query this module runs.
_FALLBACK_STOPWORDS = frozenset(
    "the and for are but not you all can had was one our out has from with "
    "this that they will have been what when which while".split()
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# The single-query description render (issue athenaeum#1344, carried
# forward per athenaeum#1358's scope note): collapses any embedded
# tab/newline/CR to a space BEFORE the value leaves SQL, and clamps to 200
# *characters* (SQLite's substr/length are UTF-8-codepoint-aware for TEXT,
# not byte-aware) on a character boundary. Both must happen in SQL, in this
# order (sanitise-then-clamp), not in Python after the fact: doing it in
# Python risks reading the value with a different codec than the DB used,
# and doing clamp-then-sanitise (instead of sanitise-then-clamp) can cut a
# multi-byte sequence in half at the 200-char boundary before the collapse
# ever runs. Reused verbatim for every row read below, so the FTS5 branch
# can never disagree with itself about what a description renders as.
_DESC_EXPR = (
    "substr(replace(replace(replace(description, char(9), ' '), "
    "char(10), ' '), char(13), ' '), 1, 200)"
)


# Kill switch (issue athenaeum#379), reimplemented inline rather than
# imported from :mod:`athenaeum.killswitch`. Not a duplication of
# convenience: `athenaeum.killswitch` is a submodule of the `athenaeum`
# package, and Python must run `athenaeum/__init__.py` before any submodule
# import can complete — even athenaeum#1360's lazy-librarian fix leaves that
# `__init__.py` pulling in `athenaeum.models`/`athenaeum.store` (pydantic +
# yaml), which alone costs most of this module's ≤127ms FTS5-path budget.
# The kill-switch check runs on EVERY call (it is the very first thing
# `build_context` does), so importing `athenaeum.killswitch` here would
# force that cost unconditionally on the hot path this module exists to
# keep lean. `killswitch.py`'s own module docstring already anticipates
# exactly this: "The shell hooks in `examples/claude-code/` reimplement
# `is_disabled` with `aspect='recall'` in a few lines of bash; keep the two
# in sync." This is that same reimplementation, in Python instead of bash,
# for the same reason. Keep in sync with `athenaeum.killswitch.is_disabled`
# / `current_state` / `_env_scope` / `_read_file_state`.
_KILLSWITCH_ENV_VAR = "ATHENAEUM_DISABLED"
_KILLSWITCH_ALL = frozenset({"1", "true", "yes", "on", "all"})
_KILLSWITCH_COMPILE = frozenset({"compile"})


def _recall_disabled(cache_dir: Path) -> bool:
    """Mirrors ``athenaeum.killswitch.is_disabled("recall", cache_dir=...)``:
    disabled only when the effective scope is ``all`` (``compile`` leaves
    recall on). Env override wins over the state file."""
    raw = os.environ.get(_KILLSWITCH_ENV_VAR)
    if raw is not None:
        val = raw.strip().lower()
        if val in _KILLSWITCH_COMPILE:
            return False  # compile-only scope leaves recall on
        if val in _KILLSWITCH_ALL:
            return True
        # unrecognised/empty -> defer to the file, same as killswitch.py
    path = cache_dir / "disabled"
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    stripped = raw_text.strip()
    if not stripped:
        return True  # bare `touch $cache/disabled` -> scope "all"
    scope = "all"
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        token = str(data.get("scope", "all")).strip().lower()
        scope = token if token in ("all", "compile") else "all"
    else:
        token = stripped.splitlines()[0].strip().lower()
        scope = token if token in ("all", "compile") else "all"
    return scope == "all"


def estimate_tokens(text: str) -> int:
    """Same estimator as :func:`athenaeum.push_metrics.estimate_tokens`
    (``max(0, len(text) // 4)``), duplicated here rather than imported so
    this module never has to import :mod:`athenaeum.push_metrics` just for
    one arithmetic expression. Issue athenaeum#1362 is what wires this
    module's output *into* ``push_metrics.record_push`` for telemetry; it
    does not need this module to import that module in the other
    direction.
    """
    return max(0, len(text) // 4)


@dataclass
class Candidate:
    """One ranked page, as it will appear in the envelope's ``candidates``."""

    filename: str
    name: str
    description: str
    backend: str  # "fts5" | "vector"
    relevance: float | None  # BM25 rank for fts5; None for vector (different scale)
    memory_tier: str  # metadata only — see module docstring
    audience: str
    token_cost: int = 0

    @property
    def bullet(self) -> str:
        return f"{self.name} — {self.description}" if self.description else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "name": self.name,
            "description": self.description,
            "backend": self.backend,
            "relevance": self.relevance,
            "memory_tier": self.memory_tier,
            "audience": self.audience,
            "token_cost": self.token_cost,
        }


@dataclass
class _Schema:
    has_description: bool
    has_memory_tier: bool


def _open_ro(db_file: Path) -> sqlite3.Connection:
    """Open the index read-only, with a busy timeout.

    Read-only (``mode=ro``) so this reader never itself becomes the lock
    contender. The busy timeout matters more than it looks: an index build
    (``build_fts5_index``, or the CLI ``repair``/``ingest`` paths) can hold
    a write lock for real work, and a per-turn query racing that write is
    the NORMAL case for a background sidecar, not an edge case. Retrying
    for up to 2s inside sqlite (rather than raising immediately) absorbs
    that ordinary contention; 2s is well over this module's own FTS5-path
    wall-clock budget, but it is a bounded worst case for an uncommon race,
    not the common path — see the callers' own ``OperationalError`` handling
    for what happens if contention still outlasts it.
    """
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout = 2000")
    return conn


def _probe_schema(conn: sqlite3.Connection) -> _Schema:
    """Issue athenaeum#1344 / athenaeum#1358 — legacy-DB safety, probed ONCE per
    connection. A DB built by an older athenaeum predates the
    ``memory_tier`` (schema v4) or ``description`` columns; selecting a
    column that doesn't exist raises ``sqlite3.OperationalError``. A naive
    implementation that lets that propagate (or swallows it into an empty
    result) degrades to a total recall outage every turn for anyone on an
    un-rebuilt index — the counter-example athenaeum#1358's acceptance
    criteria names explicitly. Probing first and adapting the SELECT list
    degrades to a working, if narrower, push instead.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(wiki)").fetchall()}
    return _Schema(
        has_description="description" in cols,
        has_memory_tier="memory_tier" in cols,
    )


def _query_fts5(
    conn: sqlite3.Connection,
    schema: _Schema,
    fts_query: str,
    *,
    n: int,
    exclude: frozenset[str],
) -> list[Candidate]:
    desc_col = _DESC_EXPR if schema.has_description else "''"
    tier_col = "memory_tier" if schema.has_memory_tier else "''"
    # One query, no per-row follow-up lookup — the counter-example this
    # issue's wall-clock criterion names explicitly ("an implementation
    # that re-spawns a query per result rather than selecting `description`
    # in the same query"). `wiki MATCH ?` and the exclude list are both
    # bound parameters, never string-interpolated, so a term containing a
    # SQL metacharacter can't break out of the query.
    exclude_list = sorted(exclude)
    placeholders = ",".join("?" for _ in exclude_list)
    exclude_clause = f"AND filename NOT IN ({placeholders})" if exclude_list else ""
    sql = (
        f"SELECT filename, name, rank, audience, {tier_col}, {desc_col} "
        f"FROM wiki WHERE wiki MATCH ? {exclude_clause} ORDER BY rank LIMIT ?"
    )
    params: list[Any] = [fts_query, *exclude_list, n]
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        # Belt-and-suspenders beyond the PRAGMA probe above (e.g. an FTS5
        # query-syntax edge case, or lock contention that outlasted
        # `_open_ro`'s busy_timeout) — degrade to no FTS5 candidates rather
        # than raising out of the whole context build. Logged, not silently
        # swallowed: a silent empty-result here for "database is locked" is
        # exactly the "reads as zero forever" hazard athenaeum#1343's
        # telemetry motivation exists to catch — it must at least be
        # observable, even though this module doesn't write telemetry
        # itself (that's issue athenaeum#1362).
        log.warning("FTS5 query failed, degrading to no candidates: %s", exc)
        return []
    out: list[Candidate] = []
    for filename, name, rank, audience, memory_tier, description in rows:
        out.append(
            Candidate(
                filename=filename,
                name=name,
                description=description or "",
                backend="fts5",
                relevance=float(rank) if rank is not None else None,
                memory_tier=memory_tier or "",
                audience=audience or "",
            )
        )
    return out


def _query_vector(
    cache_dir: Path,
    query_text: str,
    *,
    n: int,
    exclude: frozenset[str],
    conn: sqlite3.Connection | None,
    schema: _Schema | None,
) -> list[Candidate]:
    """Optional vector leg. Import-deferred: :mod:`athenaeum.search`'s
    :class:`VectorBackend` (and the embedding backend it drives) is heavier
    than this module wants to pay for FTS5-only callers, so the import
    lives here, inside the function, reached only when the caller actually
    asks for the vector backend.
    """
    vector_dir = cache_dir / "wiki-vectors"
    if not vector_dir.is_dir():
        return []
    try:
        from athenaeum.search import query_vector_index
    except ImportError:
        return []
    try:
        hits = query_vector_index(query_text, cache_dir, n=n, exclude=set(exclude))
    except Exception:  # noqa: BLE001 — vector leg is best-effort, never fatal
        return []
    if not hits:
        return []
    out: list[Candidate] = []
    filenames = [h[0] for h in hits if h[0]]
    meta: dict[str, tuple[str, str, str]] = {}
    if conn is not None and schema is not None and filenames:
        desc_col = _DESC_EXPR if schema.has_description else "''"
        tier_col = "memory_tier" if schema.has_memory_tier else "''"
        placeholders = ",".join("?" for _ in filenames)
        try:
            rows = conn.execute(
                f"SELECT filename, audience, {tier_col}, {desc_col} "
                f"FROM wiki WHERE filename IN ({placeholders})",
                filenames,
            ).fetchall()
            meta = {r[0]: (r[1] or "", r[2] or "", r[3] or "") for r in rows}
        except sqlite3.OperationalError as exc:
            log.warning("vector-hit metadata lookup failed, degrading to name-only: %s", exc)
            meta = {}
    for filename, name, _score in hits:
        audience, memory_tier, description = meta.get(filename, ("", "", ""))
        out.append(
            Candidate(
                filename=filename,
                name=name,
                description=description,
                backend="vector",
                # A vector-similarity score is not a BM25 rank — a
                # different scale entirely. Recording it as `relevance`
                # would silently mix two incomparable scores, so it is
                # left None (issue athenaeum#1358 scope: "one implementation
                # the epic converges on" inherits this distinction from
                # the shell hook it replaces).
                relevance=None,
                memory_tier=memory_tier,
                audience=audience,
            )
        )
    return out


def _dedupe(candidates: list[Candidate], n: int) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in candidates:
        if not c.filename or c.filename in seen:
            continue
        seen.add(c.filename)
        out.append(c)
        if len(out) >= n:
            break
    return out


def _extract_terms(
    prompt: str,
    *,
    timeout: float,
    stopwords: frozenset[str],
    config: dict[str, Any] | None,
    use_llm: bool,
) -> list[str]:
    """Term extraction: LLM (via the already import-light
    :func:`athenaeum.query_topics.extract_topics`) with a regex+stopword
    fallback. The LLM path is reached lazily — nothing above this function
    call imports :mod:`athenaeum.query_topics` at module scope.
    """
    terms: list[str] = []
    if use_llm:
        try:
            from athenaeum.query_topics import extract_topics

            raw_terms = extract_topics(prompt, timeout=timeout, config=config)
        except Exception:  # noqa: BLE001 — LLM path is best-effort
            raw_terms = []
        seen: set[str] = set()
        for t in raw_terms:
            for tok in _TOKEN_RE.findall(t.lower()):
                if len(tok) >= 3 and tok not in seen:
                    seen.add(tok)
                    terms.append(tok)
        terms = terms[:8]

    if terms:
        return terms

    seen = set()
    fallback: list[str] = []
    for tok in _TOKEN_RE.findall(prompt.lower()):
        if len(tok) < 3 or tok in stopwords or tok in seen:
            continue
        seen.add(tok)
        fallback.append(tok)
        if len(fallback) >= 8:
            break
    return fallback


def build_fts_query(terms: list[str]) -> str:
    """``"term1" OR "term2" OR ...`` — quoted so each term is matched as a
    phrase (relevant for multi-word LLM-extracted topics too)."""
    return " OR ".join(f'"{t}"' for t in terms)


def render_text(candidates: list[Candidate]) -> str:
    """Host-neutral rendered text: one bullet per candidate, name-only when
    a candidate has no description (no dangling separator)."""
    if not candidates:
        return ""
    lines = [f"  - {c.bullet}" for c in candidates]
    return "\n".join(lines)


def _apply_budget(candidates: list[Candidate], budget: int, preamble: str) -> list[Candidate]:
    """Greedy pack, mirroring :func:`athenaeum.memory_tiers.select_for_push`'s
    behaviour over already relevance-ordered, deduped candidates: a
    candidate is included, and its cost added to the running total, ONLY if
    doing so keeps the total within budget. A candidate that would exceed
    it is SKIPPED (never truncated) — later, smaller candidates are still
    considered.
    """
    total = estimate_tokens(preamble)
    out: list[Candidate] = []
    for c in candidates:
        block = f"  - {c.bullet}\n"
        cost = estimate_tokens(block)
        if total + cost > budget:
            continue
        total += cost
        c.token_cost = cost
        out.append(c)
    return out


PREAMBLE = (
    "[Knowledge context] Wiki pages relevant to this message "
    "(use `recall` MCP tool for full details):"
)


def build_context(
    prompt: str,
    session_id: str,
    *,
    cache_dir: Path,
    n: int = 3,
    budget: int | None = None,
    search_backend: str = "fts5",
    exclude: frozenset[str] = frozenset(),
    stopwords: frozenset[str] | None = None,
    use_llm: bool = True,
    llm_timeout: float = 3.0,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one context envelope: ranked candidates plus rendered text.

    Returns the envelope dict directly (schema owned/versioned by issue
    athenaeum#1359) — this function does no I/O beyond reading the FTS5/vector
    index under ``cache_dir``; it does not write a session-dedup file, a
    push-telemetry record, or anything else. Callers (the CLI wiring in
    ``_cmd_context.py``, or an adapter) own those side effects, using
    ``exclude`` (input) and the returned ``candidates`` (output) as the
    seam.
    """
    t0 = time.monotonic()
    budget = budget if budget is not None else DEFAULT_BUDGET_TOKENS

    # Kill switch (issue athenaeum#379), honoured at entry. See
    # `_recall_disabled`'s own docstring for why this is a same-module
    # reimplementation rather than `from athenaeum.killswitch import
    # is_disabled` — that import would force the package-root cost this
    # module's wall-clock budget cannot afford, on every single call.
    if _recall_disabled(cache_dir):
        return _empty_envelope(prompt, session_id, budget, search_backend, t0)

    db_file = cache_dir / "wiki-index.db"
    stopwords = stopwords if stopwords is not None else _FALLBACK_STOPWORDS

    terms = _extract_terms(
        prompt, timeout=llm_timeout, stopwords=stopwords, config=config, use_llm=use_llm
    )

    fts_candidates: list[Candidate] = []
    vector_candidates: list[Candidate] = []
    conn: sqlite3.Connection | None = None
    schema: _Schema | None = None

    if terms and db_file.is_file():
        conn = _open_ro(db_file)
        try:
            schema = _probe_schema(conn)
            fts_query = build_fts_query(terms)
            fts_candidates = _query_fts5(conn, schema, fts_query, n=n, exclude=exclude)

            if search_backend == "vector":
                vector_query = " ".join(terms) or prompt
                vector_candidates = _query_vector(
                    cache_dir,
                    vector_query,
                    n=n,
                    exclude=exclude,
                    conn=conn,
                    schema=schema,
                )
        finally:
            conn.close()
    elif terms and search_backend == "vector" and (cache_dir / "wiki-vectors").is_dir():
        vector_query = " ".join(terms) or prompt
        vector_candidates = _query_vector(
            cache_dir, vector_query, n=n, exclude=exclude, conn=None, schema=None
        )

    # Merge: FTS5 first (lexical precision), then vector, dedupe, cap n.
    # Selection/ordering are relevance-alone here — nothing above orders by
    # `memory_tier` (issue athenaeum#1345's invariant; see module docstring).
    merged = _dedupe([*fts_candidates, *vector_candidates], n)
    packed = _apply_budget(merged, budget, PREAMBLE)

    return _make_envelope(prompt, session_id, budget, search_backend, packed, t0)


def _make_envelope(
    prompt: str,
    session_id: str,
    budget: int,
    search_backend: str,
    candidates: list[Candidate],
    t0: float,
) -> dict[str, Any]:
    return {
        "v": ENVELOPE_VERSION,
        "query": prompt,
        "session_id": session_id,
        "candidates": [c.to_dict() for c in candidates],
        "budget": {
            "tokens": budget,
            "used": sum(c.token_cost for c in candidates),
        },
        "render": {
            "text": render_text(candidates),
            "preamble": PREAMBLE,
        },
        "backend": search_backend,
        "elapsed_ms": (time.monotonic() - t0) * 1000,
    }


def _empty_envelope(
    prompt: str, session_id: str, budget: int, search_backend: str, t0: float
) -> dict[str, Any]:
    """Issue athenaeum#379 kill-switch short-circuit: same envelope shape,
    zero candidates, so a caller never has to special-case a disabled turn.
    """
    return _make_envelope(prompt, session_id, budget, search_backend, [], t0)
