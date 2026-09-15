# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1620: T1 screen coverage — the structural repro, the deliberate-
unscreened writers, and the screened-vs-unscreened census.

- ``TestT1ScreenDominatesWrite`` — AC1(a): proves, structurally, that
  ``merge.py``'s cluster path CANNOT reach ``write_pending_merge`` without
  first passing through ``t1_screen_rejects_merge_proposal`` — i.e. the
  issue's first hypothesis ("a code path reaches merge.py:2096 without
  passing merge.py:2037") is FALSE against today's code, pinned so a future
  edit cannot silently reorder the two calls.

  AC1(b) — the REASON the T1 screen made no call on the measured window
  (the screen returns early, and every reason but the spend ceiling was
  previously silent) — is reproduced in ``tests/test_merge_reasoning_wiring.py``
  ``TestT1ScreenGuards``, not here: that is where the screen's own guard
  logic already lived under test.

- ``TestDeliberatelyUnscreenedWriters`` — AC2: each of
  ``name_collisions.py`` / ``name_structure.py`` carries a code comment,
  next to its own ``write_pending_merge`` call, stating why it is
  deliberately unscreened by T1, and citing athenaeum#1620.

- ``TestT1Census`` / ``TestT1CensusIntegration`` — AC3: the run-scoped
  screened-vs-unscreened census (``athenaeum.t1_census``), and its wiring
  through both writers into ``RunContext.emit_run_summary``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from athenaeum.librarian import (
    _render_run_summary,
    _run_name_collision_phase,
    _run_qualified_name_phase,
)
from athenaeum.run_summary_log import build_run_summary_ledger_record
from athenaeum.t1_census import (
    T1_UNSCREENED_NAME_COLLISION,
    T1_UNSCREENED_NAME_STRUCTURE,
    T1Census,
    get_t1_census,
)
from tests.test_librarian_run_phases import _make_ctx
from tests.test_name_collisions_1170 import _write_page

_REPO_SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"


# ---------------------------------------------------------------------------
# AC1(a) — structural repro: the T1 screen call dominates the write
# ---------------------------------------------------------------------------


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no top-level-or-nested function named {name!r} found")


def _calls_named(node: ast.AST, name: str):
    """Every ``ast.Call`` anywhere inside *node* whose called name is *name*
    (bare ``name(...)`` or ``obj.name(...)``)."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            if isinstance(func, ast.Name) and func.id == name:
                yield n
            elif isinstance(func, ast.Attribute) and func.attr == name:
                yield n


def _iter_blocks(node: ast.AST):
    """Yield every statement-list ("block") anywhere under *node* — each
    ``if``/``try``/``for``/``while``/``with``/function body, ``orelse``,
    ``finalbody``, and each ``except`` handler's body. Both calls this test
    cares about may sit at ANY nesting depth (e.g. inside an outer
    ``if proposal ...:``), so "dominates" has to be checked against the
    actual shared block the two calls are direct-or-nested-child statements
    of, not assumed to be the function's own top-level body.
    """
    for n in ast.walk(node):
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(n, attr, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                yield block
        for handler in getattr(n, "handlers", None) or []:
            if isinstance(handler.body, list) and handler.body:
                yield handler.body


class TestT1ScreenDominatesWrite:
    def test_t1_screen_call_dominates_write_pending_merge_in_emit_escalation(
        self,
    ) -> None:
        """Issue athenaeum#1620 AC1(a): within ``merge.py``'s ``_emit_escalation``
        (the function housing the cluster-path's ``PROPOSE_MERGE_ACTION``
        branch), the ``t1_screen_rejects_merge_proposal`` call must appear
        as an early-return GUARD, structurally BEFORE the
        ``write_pending_merge`` call — i.e. every straight-line path that
        reaches the write has already passed the screen. This is the
        static, AST-level version of "dominates": a brittle line-number
        assertion would not survive an unrelated edit; this survives any
        edit that does not actually reorder the two calls.
        """
        tree = ast.parse((_REPO_SRC / "merge.py").read_text(encoding="utf-8"))
        func = _find_function(tree, "_emit_escalation")

        # Find the block that contains BOTH a t1-screen-guard statement and
        # a write_pending_merge-call statement as two DISTINCT immediate
        # elements — that is the block "dominates" has to be checked
        # within, regardless of how deeply nested it is inside
        # _emit_escalation as a whole. `_iter_blocks` yields coarse
        # (outer) blocks before fine (inner) ones, and a coarse block's
        # single top-level statement can recursively CONTAIN both calls
        # without being a guard at all (e.g. the whole `if proposal...:`
        # wrapper) — that is a false match (``ti == wi``, the same
        # statement satisfying both searches), so candidates are collected
        # and the SMALLEST qualifying block (the most specific one, where
        # the two calls are genuinely separate sibling statements) wins.
        candidates: list[tuple[int, int, int, ast.stmt]] = []  # (blocklen, ti, wi, guard_stmt)
        for block in _iter_blocks(func):
            ti = wi = None
            guard_stmt: ast.stmt | None = None
            for i, stmt in enumerate(block):
                if ti is None and list(
                    _calls_named(stmt, "t1_screen_rejects_merge_proposal")
                ):
                    ti = i
                    guard_stmt = stmt
                if wi is None and list(_calls_named(stmt, "write_pending_merge")):
                    wi = i
            if ti is not None and wi is not None and ti != wi:
                candidates.append((len(block), ti, wi, guard_stmt))

        assert candidates, (
            "no shared block found in _emit_escalation containing both a "
            "t1_screen_rejects_merge_proposal guard and a write_pending_merge "
            "call as distinct statements — has the screen or the write been "
            "removed or moved?"
        )
        _blocklen, t1_index, write_index, guard_stmt = min(candidates, key=lambda c: c[0])
        # Must be an early-return guard: an `if <...t1 call...>:` whose body
        # unconditionally returns — not merely a call whose result is
        # discarded.
        assert isinstance(guard_stmt, ast.If), (
            "t1_screen_rejects_merge_proposal is called, but not as an "
            "`if ...: return` guard clause"
        )
        assert any(isinstance(s, ast.Return) for s in guard_stmt.body), (
            "the t1_screen_rejects_merge_proposal `if` block does not "
            "contain a `return` — it would not actually stop the write below"
        )
        assert t1_index < write_index, (
            "write_pending_merge is reachable in _emit_escalation without "
            "first passing the t1_screen_rejects_merge_proposal guard — "
            "this is the exact regression issue athenaeum#1620 AC1 asks to be "
            "pinned against"
        )


# ---------------------------------------------------------------------------
# AC2 — the two deliberately-unscreened writers document why, in code
# ---------------------------------------------------------------------------


class TestDeliberatelyUnscreenedWriters:
    def test_name_collisions_write_site_documents_deliberate_unscreening(self) -> None:
        text = (_REPO_SRC / "name_collisions.py").read_text(encoding="utf-8")
        idx = text.index("write_pending_merge(")
        preceding = text[:idx]
        # The comment block immediately above the call, not just anywhere
        # in the file.
        window = preceding[-1500:]
        assert "athenaeum#1620" in window
        assert "deliberately unscreened" in window.lower()

    def test_name_structure_write_site_documents_deliberate_unscreening(self) -> None:
        text = (_REPO_SRC / "name_structure.py").read_text(encoding="utf-8")
        idx = text.index("write_pending_merge(")
        preceding = text[:idx]
        window = preceding[-1500:]
        assert "athenaeum#1620" in window
        assert "deliberately unscreened" in window.lower()


# ---------------------------------------------------------------------------
# AC3 — the census itself
# ---------------------------------------------------------------------------


class TestT1Census:
    def test_fresh_census_is_zero(self) -> None:
        census = T1Census()
        assert census.screened == 0
        assert census.unscreened == 0
        assert census.unscreened_by_reason == {}

    def test_record_and_reset(self) -> None:
        census = T1Census()
        census.record_screened()
        census.record_screened()
        census.record_unscreened("disabled")
        census.record_unscreened("disabled")
        census.record_unscreened("ceiling")
        assert census.screened == 2
        assert census.unscreened == 3
        assert census.unscreened_by_reason == {"disabled": 2, "ceiling": 1}
        census.reset()
        assert census.screened == 0
        assert census.unscreened == 0
        assert census.unscreened_by_reason == {}

    def test_as_profile_fields_renders_sorted_reasons(self) -> None:
        census = T1Census()
        census.record_screened()
        census.record_unscreened("zzz")
        census.record_unscreened("aaa")
        fields = census.as_profile_fields()
        assert fields["screened"] == 1
        assert fields["unscreened"] == 2
        assert fields["unscreened_reasons"] == "aaa:1,zzz:1"
        assert fields["reason"] == "completed"

    def test_as_profile_fields_omits_reasons_key_when_nothing_unscreened(self) -> None:
        census = T1Census()
        census.record_screened()
        fields = census.as_profile_fields()
        assert "unscreened_reasons" not in fields

    def test_get_t1_census_is_the_process_global_singleton(self) -> None:
        get_t1_census().record_screened()
        assert get_t1_census().screened == 1
        get_t1_census().record_screened()
        assert get_t1_census().screened == 2
        # tests/conftest.py's autouse _reset_t1_census fixture resets this
        # after THIS test returns — no manual reset needed here.


# ---------------------------------------------------------------------------
# AC3 — wired through both deliberately-unscreened writers, and into the
# run summary / durable ledger record
# ---------------------------------------------------------------------------


class TestT1CensusIntegration:
    def test_name_collision_write_increments_deliberate_reason(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(
            wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Real content."
        )
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=False)
        _run_name_collision_phase(ctx)
        assert get_t1_census().unscreened_by_reason == {T1_UNSCREENED_NAME_COLLISION: 1}
        assert get_t1_census().screened == 0

    def test_qualified_name_write_increments_deliberate_reason(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_page(wiki, "widget.md", uid="u1", name="Widget", type_="concept", body="A body.")
        _write_page(
            wiki,
            "widget-variant.md",
            uid="u2",
            name="Widget (Variant)",
            type_="concept",
            body="A variant body.",
        )
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=False)
        _run_qualified_name_phase(ctx)
        assert get_t1_census().unscreened_by_reason == {T1_UNSCREENED_NAME_STRUCTURE: 1}
        assert get_t1_census().screened == 0

    def test_both_deliberate_writers_split_correctly_in_one_run(self, tmp_path: Path) -> None:
        """A run that writes proposals through both deliberately-unscreened
        writers reports the right split (issue athenaeum#1620 AC3's own worked
        example)."""
        wiki = tmp_path / "wiki"
        _write_page(
            wiki, "acme.md", uid="u1", name="Acme", type_="company", body="Real content."
        )
        _write_page(wiki, "acme-dup.md", uid="u2", name="Acme", type_="company", body="")
        _write_page(wiki, "widget.md", uid="u3", name="Widget", type_="concept", body="A body.")
        _write_page(
            wiki,
            "widget-variant.md",
            uid="u4",
            name="Widget (Variant)",
            type_="concept",
            body="A variant body.",
        )
        ctx = _make_ctx(tmp_path, wiki_root=wiki, dry_run=False)
        _run_name_collision_phase(ctx)
        _run_qualified_name_phase(ctx)

        census = get_t1_census()
        assert census.screened == 0
        assert census.unscreened_by_reason == {
            T1_UNSCREENED_NAME_COLLISION: 1,
            T1_UNSCREENED_NAME_STRUCTURE: 1,
        }

    def test_emit_run_summary_appends_t1_screen_phase_entry(self, tmp_path: Path) -> None:
        """The census flows into ``ctx.run_profile`` — and therefore into
        BOTH the prose ``librarian-run-summary`` line and the durable JSONL
        ledger record — with no further wiring than
        ``RunContext.emit_run_summary`` itself (issue athenaeum#1620 AC3)."""
        get_t1_census().record_screened()
        get_t1_census().record_unscreened(T1_UNSCREENED_NAME_COLLISION)
        get_t1_census().record_unscreened(T1_UNSCREENED_NAME_COLLISION)

        ctx = _make_ctx(tmp_path)
        ctx.emit_run_summary()

        entries = [e for e in ctx.run_profile if e[0] == "t1-screen"]
        assert len(entries) == 1
        _name, secs, fields = entries[0]
        assert secs == 0.0
        assert fields["screened"] == 1
        assert fields["unscreened"] == 2
        assert fields["unscreened_reasons"] == f"{T1_UNSCREENED_NAME_COLLISION}:2"
        assert fields["reason"] == "completed"

        # Same input flows into the durable ledger record unchanged.
        record = build_run_summary_ledger_record(ctx.run_profile)
        assert record["phases"]["t1-screen"]["screened"] == 1
        assert record["phases"]["t1-screen"]["unscreened"] == 2

        # ...and into the greppable prose line.
        line = _render_run_summary(ctx.run_profile)
        assert "t1-screen secs=0.000 screened=1 unscreened=2" in line

    def test_emit_run_summary_is_idempotent_census_read_once(self, tmp_path: Path) -> None:
        """``emit_run_summary`` is guarded by ``summary_emitted`` — a second
        call must not append a second ``t1-screen`` entry (mirrors the
        guard's existing contract for every other phase entry it emits)."""
        get_t1_census().record_screened()
        ctx = _make_ctx(tmp_path)
        ctx.emit_run_summary()
        ctx.emit_run_summary()
        entries = [e for e in ctx.run_profile if e[0] == "t1-screen"]
        assert len(entries) == 1

    def test_emit_run_summary_omits_t1_screen_entry_when_census_is_all_zero(
        self, tmp_path: Path
    ) -> None:
        """A run that never reached any merge-proposal writer (census still
        all-zero) must not gain a hollow ``t1-screen`` entry — several
        existing tests assert an exactly-empty ``run_profile`` for a run
        with nothing to report, and this mirrors the
        ``economics``/``alerts`` "omit, don't report a zero" convention."""
        ctx = _make_ctx(tmp_path)
        ctx.emit_run_summary()
        assert ctx.run_profile == []
