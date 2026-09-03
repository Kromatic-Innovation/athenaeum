# SPDX-License-Identifier: Apache-2.0
"""T1 wired into the cluster-domain comparator lane, T2 deliberately NOT (athenaeum#1257).

Issue athenaeum#1257 re-sited the two reasoning screens that retiring
``merge.py``'s C4 detector (issue athenaeum#1256) would orphan. Both now live
in :mod:`athenaeum.reasoning_screens`; only T1 is wired into
:func:`athenaeum.cluster_comparator.run_cluster_comparator`.

The asymmetry is the point, and it is the thing these tests pin.
:func:`~athenaeum.cluster_comparator.run_cluster_comparator` produces
:class:`~athenaeum.comparator.CompareOutcome` objects: it never calls
:func:`athenaeum.pending_merges.write_pending_merge` and it fabricates neither
a ``confidence`` scalar nor a ``draft_merged_body``. T1 needs neither, so it
wires in. T2's auto-finalize path needs both, and inventing them here is the
anti-pattern athenaeum#658 finding D2 recorded and athenaeum#715 banned — so
T2 relocated WITH T1 but keeps only its existing C4 call site.

Every "client" here is a ``unittest.mock.MagicMock``, matching
``tests/test_cluster_comparator.py``. No network; no filesystem outside
``tmp_path``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum import cluster_comparator as cc_mod
from athenaeum.cluster_comparator import (
    ClusterComparatorResult,
    ClusterScreenContext,
    run_cluster_comparator,
)
from athenaeum.models import AutoMemoryFile, TokenUsage
from athenaeum.verdicts import page_id_for_path

_SRC = Path(cc_mod.__file__)
_MERGE_SRC = _SRC.parent / "merge.py"

# comparator gate on; T1's OWN knob is a SEPARATE key and stays off here.
_COMPARATOR_ON: dict[str, object] = {"librarian": {"comparator_enabled": True}}
# comparator gate on AND T1's own opt-in on, with a sample rate high enough
# that a reject always reaches the calibration ledger.
_COMPARATOR_ON_T1_ON: dict[str, object] = {
    "librarian": {
        "comparator_enabled": True,
        "reasoning_tier_auditing_enabled": True,
        "audit_sample_rate_t1_rejects": 1.0,
    }
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_am(
    scope_dir: Path,
    filename: str,
    body: str,
    *,
    memory_class: str | None = None,
) -> AutoMemoryFile:
    """A real-on-disk :class:`AutoMemoryFile`.

    ``memory_class`` is what T1's DETERMINISTIC cross-class check reads: two
    members with distinct, non-empty classes are a confident reject with no
    model call at all (see ``tests/test_merge_reasoning_wiring.py``).
    """
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    lines = ["---", f"name: {filename}", "type: feedback"]
    if memory_class is not None:
        lines.append(f"memory_class: {memory_class}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="utf-8")
    return AutoMemoryFile(
        path=path,
        origin_scope="scope-x",
        memory_type="feedback",
        name=filename,
    )


def _comparator_client() -> MagicMock:
    """A client canned to resolve the comparator's Gate 2 to ``compatible``."""
    payload = json.dumps(
        {
            "content_relation": "compatible",
            "conflicting_passages": [],
            "predicate_a": "a-predicate",
            "predicate_b": "b-predicate",
            "rationale": "test rationale",
        }
    )
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload)]
    client.messages.create.return_value = response
    return client


def _t1_passup_client() -> MagicMock:
    """A T1 client canned to PASS UP — the verdict that leaves a pair to be
    compared normally."""
    client = MagicMock()
    response = MagicMock()
    response.content = [
        MagicMock(text='{"verdict": "pass_up", "reason": "not confident"}')
    ]
    client.messages.create.return_value = response
    return client


def _screen_ctx(tmp_path: Path, **overrides: object) -> ClusterScreenContext:
    wiki = tmp_path / "wiki"
    wiki.mkdir(exist_ok=True)
    kwargs: dict[str, object] = {
        "wiki_root": wiki,
        "knowledge_root": tmp_path,
        "provider": "claude-cli",
        "client": None,
        "merge_target_name": "Alpha",
        "dry_run": False,
    }
    kwargs.update(overrides)
    return ClusterScreenContext(**kwargs)  # type: ignore[arg-type]


def _cross_class_pair(tmp_path: Path) -> list[AutoMemoryFile]:
    """Two members T1 rejects DETERMINISTICALLY (distinct ``memory_class``)."""
    scope = tmp_path / "raw" / "auto-memory" / "scope-x"
    return [
        _write_am(scope, "alpha.md", "text a", memory_class="fact"),
        _write_am(scope, "beta.md", "text b", memory_class="guideline"),
    ]


def _same_class_pair(tmp_path: Path) -> list[AutoMemoryFile]:
    """Two members T1's deterministic checks pass, so the model decides."""
    scope = tmp_path / "raw" / "auto-memory" / "scope-x"
    return [
        _write_am(scope, "alpha.md", "text a", memory_class="fact"),
        _write_am(scope, "beta.md", "text b", memory_class="fact"),
    ]


# ---------------------------------------------------------------------------
# AC1 / AC3 -- where each screen is DEFINED, and where each is CALLED
# ---------------------------------------------------------------------------


class TestScreenSiting:
    def test_neither_screen_is_defined_in_merge(self) -> None:
        """AC1: ``merge.py`` no longer DEFINES either screen.

        The AC's own check, mechanically: zero ``def`` lines for either name.
        ``merge.py`` still REACHES both by import (asserted below) — this is
        about the definition site, not the call sites.
        """
        tree = ast.parse(_MERGE_SRC.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert "t1_screen_rejects_merge_proposal" not in defined
        assert "t2_screen_merge_proposal" not in defined

    def test_merge_still_reaches_both_screens_by_import(self) -> None:
        """AC6: C4's call sites are unchanged — both names still resolve in
        ``merge``'s namespace, and they are the SAME objects the new home
        exports (not a re-definition or a shim)."""
        from athenaeum import merge as merge_mod
        from athenaeum import reasoning_screens as screens_mod

        assert (
            merge_mod.t1_screen_rejects_merge_proposal
            is screens_mod.t1_screen_rejects_merge_proposal
        )
        assert (
            merge_mod.t2_screen_merge_proposal is screens_mod.t2_screen_merge_proposal
        )

    def test_cluster_comparator_does_not_import_merge(self) -> None:
        """AC1: the screens are reachable from ``cluster_comparator`` WITHOUT
        importing ``merge`` — the whole point of the move, since athenaeum#1256
        retires ``merge``'s C4 lane."""
        tree = ast.parse(_SRC.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "athenaeum.merge" not in imported
        assert not any(m.startswith("athenaeum.merge.") for m in imported)

    def test_t2_has_no_call_site_in_cluster_comparator(self) -> None:
        """AC3: T2 is relocated with T1 but is NOT called from the comparator
        lane, because that lane produces no ``confidence`` and no
        ``draft_merged_body`` and must not invent them (athenaeum#658 D2,
        athenaeum#715).

        Checked two ways: the name is absent from the module's namespace at
        all (so no import path can reach it), and the AST carries no call to
        it (so no deferred/function-local import could smuggle one in).
        """
        assert not hasattr(cc_mod, "t2_screen_merge_proposal")

        tree = ast.parse(_SRC.read_text(encoding="utf-8"))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        assert "t2_screen_merge_proposal" not in called
        assert "t1_screen_rejects_merge_proposal" in called  # positive control


# ---------------------------------------------------------------------------
# AC2 -- T1 on the cluster-domain pair path, behind the comparator gate
# ---------------------------------------------------------------------------


class TestT1NotArmed:
    def test_no_screen_context_runs_no_screen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default posture: ``screen=None`` never calls T1, so every pair
        is compared exactly as it was before athenaeum#1257."""
        calls: list[object] = []
        monkeypatch.setattr(
            cc_mod, "t1_screen_rejects_merge_proposal", lambda **kw: calls.append(kw)
        )
        members = _cross_class_pair(tmp_path)

        result = run_cluster_comparator(
            members, _comparator_client(), config=_COMPARATOR_ON, cluster_id="c1"
        )

        assert calls == []
        assert result.screened_out == []
        assert len(result.outcomes) == 1

    def test_screen_context_without_t1_knob_runs_no_screen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Arming the COMPARATOR does not arm the SCREEN. T1 keeps its own
        default-OFF ``reasoning_tier_auditing_enabled`` knob (athenaeum#1200),
        whose name and default this issue leaves untouched."""
        calls: list[object] = []
        monkeypatch.setattr(
            cc_mod, "t1_screen_rejects_merge_proposal", lambda **kw: calls.append(kw)
        )
        members = _cross_class_pair(tmp_path)

        result = run_cluster_comparator(
            members,
            _comparator_client(),
            config=_COMPARATOR_ON,  # comparator on, T1 knob absent -> off
            cluster_id="c1",
            screen=_screen_ctx(tmp_path),
        )

        assert calls == []
        assert result.screened_out == []
        assert len(result.outcomes) == 1

    def test_comparator_gate_off_runs_no_screen_even_when_armed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2's byte-identical clause. With ``resolve_comparator_enabled``
        off, the driver returns before the pair loop — so a fully armed screen
        context still produces no T1 call, no content read, and no model
        call."""
        calls: list[object] = []
        monkeypatch.setattr(
            cc_mod, "t1_screen_rejects_merge_proposal", lambda **kw: calls.append(kw)
        )
        client = _comparator_client()
        members = _cross_class_pair(tmp_path)

        result = run_cluster_comparator(
            members,
            client,
            config=None,  # default: comparator OFF
            cluster_id="c1",
            screen=_screen_ctx(tmp_path),
        )

        assert calls == []
        assert result.gate_enabled is False
        assert result.pair_count == 1
        assert result.outcomes == []
        assert result.screened_out == []
        client.messages.create.assert_not_called()


class TestT1Armed:
    def test_confident_reject_drops_the_pair_before_any_comparison(
        self, tmp_path: Path
    ) -> None:
        """The cluster-domain analogue of C4's "drop before the human queue":
        a confident T1 reject means the pair is never compared."""
        members = _cross_class_pair(tmp_path)
        client = _comparator_client()

        result = run_cluster_comparator(
            members,
            client,
            config=_COMPARATOR_ON_T1_ON,
            cluster_id="c1",
            screen=_screen_ctx(tmp_path, client=MagicMock()),
        )

        assert result.gate_enabled is True
        assert result.pair_count == 1
        assert result.outcomes == []  # never compared
        assert result.screened_out == [
            (page_id_for_path(members[0].path), page_id_for_path(members[1].path))
        ]
        # The comparator's own client was never asked to compare anything.
        client.messages.create.assert_not_called()

    def test_reject_costs_no_member_content_read(self, tmp_path: Path) -> None:
        """T1 screens on PATHS, before ``page_from_auto_memory_file`` reads
        either member's body. A member whose file T1 itself never needs to
        parse in full would still be read by the adapter — proving the screen
        runs first is what this pins."""
        members = _cross_class_pair(tmp_path)
        reads: list[Path] = []
        original = cc_mod.page_from_auto_memory_file

        def _spy(member: AutoMemoryFile):  # type: ignore[no-untyped-def]
            reads.append(member.path)
            return original(member)

        cc_mod.page_from_auto_memory_file = _spy  # type: ignore[assignment]
        try:
            result = run_cluster_comparator(
                members,
                _comparator_client(),
                config=_COMPARATOR_ON_T1_ON,
                cluster_id="c1",
                screen=_screen_ctx(tmp_path, client=MagicMock()),
            )
        finally:
            cc_mod.page_from_auto_memory_file = original  # type: ignore[assignment]

        assert result.screened_out != []
        assert reads == []

    def test_pass_up_flows_through_to_the_comparison(self, tmp_path: Path) -> None:
        """A T1 pass-up is NOT a drop: the pair is compared exactly as it
        would be with no screen — the same degrade-to-unscreened contract T1
        has always had on the C4 lane."""
        members = _same_class_pair(tmp_path)
        t1_client = _t1_passup_client()

        result = run_cluster_comparator(
            members,
            _comparator_client(),
            config=_COMPARATOR_ON_T1_ON,
            cluster_id="c1",
            screen=_screen_ctx(tmp_path, client=t1_client),
        )

        assert result.screened_out == []
        assert len(result.outcomes) == 1
        t1_client.messages.create.assert_called_once()

    def test_spend_ceiling_degrades_to_an_unscreened_comparison(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-safe direction, inherited verbatim from the screen: a tripped
        ceiling never DROPS a pair, it just leaves it unscreened."""
        from athenaeum import reasoning_screens as screens_mod

        monkeypatch.setattr(
            screens_mod.spend, "ceiling_tripped", lambda *a, **k: "budget"
        )
        members = _cross_class_pair(tmp_path)  # would otherwise be rejected

        result = run_cluster_comparator(
            members,
            _comparator_client(),
            config=_COMPARATOR_ON_T1_ON,
            usage=TokenUsage(),
            cluster_id="c1",
            screen=_screen_ctx(tmp_path, client=MagicMock()),
        )

        assert result.screened_out == []
        assert len(result.outcomes) == 1

    def test_screen_client_falls_back_to_the_comparator_client(
        self, tmp_path: Path
    ) -> None:
        """``ClusterScreenContext.client`` is the ``reasoning_t1`` knob's own
        client (issue athenaeum#841). ``None`` falls back to the run-level
        client, mirroring ``merge.merge_clusters_to_wiki``'s own
        ``reasoning_t1_client if ... is not None else client``."""
        members = _cross_class_pair(tmp_path)

        result = run_cluster_comparator(
            members,
            _comparator_client(),
            config=_COMPARATOR_ON_T1_ON,
            cluster_id="c1",
            screen=_screen_ctx(tmp_path, client=None),
        )

        # A deterministic cross-class reject still fires on the fallback
        # client — the screen was reached, not skipped for want of a client.
        assert result.screened_out != []

    def test_only_the_rejected_pair_is_dropped(self, tmp_path: Path) -> None:
        """A three-member cluster with one cross-class member: the two pairs
        containing it drop, the remaining same-class pair is compared."""
        scope = tmp_path / "raw" / "auto-memory" / "scope-x"
        a = _write_am(scope, "alpha.md", "text a", memory_class="fact")
        b = _write_am(scope, "beta.md", "text b", memory_class="fact")
        c = _write_am(scope, "gamma.md", "text c", memory_class="guideline")

        # (a, b) are same-class, so T1's deterministic checks pass and the
        # model decides — canned to pass_up so only the CROSS-class pairs
        # containing ``c`` are dropped.
        result = run_cluster_comparator(
            [a, b, c],
            _comparator_client(),
            config=_COMPARATOR_ON_T1_ON,
            cluster_id="c1",
            screen=_screen_ctx(tmp_path, client=_t1_passup_client()),
        )

        assert result.pair_count == 3
        assert len(result.screened_out) == 2
        assert len(result.outcomes) == 1
        compared = {result.outcomes[0][0], result.outcomes[0][1]}
        assert compared == {page_id_for_path(a.path), page_id_for_path(b.path)}


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


class TestScreenedOutRow:
    def test_to_row_carries_screened_out(self, tmp_path: Path) -> None:
        members = _cross_class_pair(tmp_path)

        row = run_cluster_comparator(
            members,
            _comparator_client(),
            config=_COMPARATOR_ON_T1_ON,
            cluster_id="c9",
            screen=_screen_ctx(tmp_path, client=MagicMock()),
        ).to_row()

        assert row["cluster_id"] == "c9"
        assert row["pair_count"] == 1
        assert row["outcomes"] == []
        assert row["screened_out"] == [
            {
                "a": page_id_for_path(members[0].path),
                "b": page_id_for_path(members[1].path),
            }
        ]

    def test_to_row_screened_out_is_empty_when_nothing_was_screened(self) -> None:
        row = ClusterComparatorResult(
            cluster_id="c0", pair_count=3, gate_enabled=False
        ).to_row()
        assert row["screened_out"] == []
