"""Unit tests for the ephemeral/operational classifier (issue #278).

Covers :func:`athenaeum.ephemeral.classify_ephemeral` (raw intake),
:func:`athenaeum.ephemeral.classify_ephemeral_page` (compiled wiki page),
and the config resolvers
:func:`athenaeum.config.resolve_ephemeral_scopes` /
:func:`athenaeum.config.resolve_operational_markers`.

These pin the precision order (flag > scope-glob > multi-signal markers) and
the conservative defaults (markers default-empty; legit notes never dropped).
"""

from __future__ import annotations

from athenaeum.config import (
    _DEFAULT_EPHEMERAL_SCOPES,
    resolve_ephemeral_scopes,
    resolve_operational_markers,
)
from athenaeum.ephemeral import classify_ephemeral, classify_ephemeral_page

LEGIT_SCOPE = "-Users-alice-Code-projectx"
EPHEMERAL_SCOPE = "-private-tmp-claude-cctest-abc123"


class TestConfigResolvers:
    def test_ephemeral_scopes_default_when_unset(self) -> None:
        assert resolve_ephemeral_scopes({}) == list(_DEFAULT_EPHEMERAL_SCOPES)
        assert resolve_ephemeral_scopes(None) == list(_DEFAULT_EPHEMERAL_SCOPES)

    def test_ephemeral_scopes_user_set_replaces_defaults(self) -> None:
        cfg = {"librarian": {"ephemeral_scopes": ["*throwaway*", "  *foo*  "]}}
        assert resolve_ephemeral_scopes(cfg) == ["*throwaway*", "*foo*"]

    def test_ephemeral_scopes_empty_list_disables(self) -> None:
        cfg = {"librarian": {"ephemeral_scopes": []}}
        assert resolve_ephemeral_scopes(cfg) == []

    def test_operational_markers_default_empty(self) -> None:
        assert resolve_operational_markers({}) == []
        assert resolve_operational_markers(None) == []

    def test_operational_markers_lowercased(self) -> None:
        cfg = {"librarian": {"operational_markers": ["Deploy", "WORKTREE"]}}
        assert resolve_operational_markers(cfg) == ["deploy", "worktree"]


class TestClassifyEphemeralRaw:
    def _scopes(self) -> list[str]:
        return list(_DEFAULT_EPHEMERAL_SCOPES)

    def test_explicit_flag_is_authoritative(self) -> None:
        reason = classify_ephemeral(
            LEGIT_SCOPE,
            {"ephemeral": True, "name": "x"},
            "real architecture note body",
            ephemeral_scopes=self._scopes(),
            operational_markers=[],
        )
        assert reason is not None
        assert "ephemeral:true" in reason

    def test_flag_string_truthy(self) -> None:
        reason = classify_ephemeral(
            LEGIT_SCOPE,
            {"ephemeral": "yes"},
            "body",
            ephemeral_scopes=self._scopes(),
            operational_markers=[],
        )
        assert reason is not None

    def test_ephemeral_scope_glob_match(self) -> None:
        reason = classify_ephemeral(
            EPHEMERAL_SCOPE,
            {"name": "anything"},
            "body",
            ephemeral_scopes=self._scopes(),
            operational_markers=[],
        )
        assert reason is not None
        assert "ephemeral scope" in reason

    def test_legit_scope_not_dropped(self) -> None:
        assert (
            classify_ephemeral(
                LEGIT_SCOPE,
                {"name": "Recall architecture", "description": "FTS5 pipeline"},
                "The recall hook surfaces wiki context via FTS5 + vector.",
                ephemeral_scopes=self._scopes(),
                operational_markers=[],
            )
            is None
        )

    def test_markers_require_multi_signal(self) -> None:
        # A single marker present -> NOT dropped (conservative).
        single = classify_ephemeral(
            LEGIT_SCOPE,
            {"name": "Deploy notes"},
            "We deploy on Fridays.",
            ephemeral_scopes=[],
            operational_markers=["deploy", "worktree", "install-token"],
        )
        assert single is None
        # Two distinct markers present -> dropped.
        multi = classify_ephemeral(
            LEGIT_SCOPE,
            {"name": "Deploy worktree boilerplate"},
            "Staging deploy on the worktree.",
            ephemeral_scopes=[],
            operational_markers=["deploy", "worktree", "install-token"],
        )
        assert multi is not None
        assert "operational markers" in multi

    def test_empty_markers_never_fire(self) -> None:
        assert (
            classify_ephemeral(
                LEGIT_SCOPE,
                {"name": "deploy worktree deploy"},
                "deploy worktree staging ci install-token",
                ephemeral_scopes=[],
                operational_markers=[],
            )
            is None
        )


class TestClassifyEphemeralPage:
    def _scopes(self) -> list[str]:
        return list(_DEFAULT_EPHEMERAL_SCOPES)

    def test_all_origin_scopes_ephemeral_killed(self) -> None:
        meta = {
            "type": "auto-memory",
            "origin_scopes": [EPHEMERAL_SCOPE, "-private-tmp-claude-cctest-xyz"],
        }
        reason = classify_ephemeral_page(
            meta, "body", ephemeral_scopes=self._scopes(), operational_markers=[]
        )
        assert reason is not None
        assert "all origin scopes ephemeral" in reason

    def test_mixed_scopes_retained(self) -> None:
        # One throwaway + one real scope -> conservative RETAIN.
        meta = {
            "type": "auto-memory",
            "origin_scopes": [EPHEMERAL_SCOPE, LEGIT_SCOPE],
        }
        assert (
            classify_ephemeral_page(
                meta, "body", ephemeral_scopes=self._scopes(), operational_markers=[]
            )
            is None
        )

    def test_legit_page_retained(self) -> None:
        meta = {"type": "auto-memory", "origin_scopes": [LEGIT_SCOPE]}
        assert (
            classify_ephemeral_page(
                meta,
                "Recall architecture details.",
                ephemeral_scopes=self._scopes(),
                operational_markers=[],
            )
            is None
        )

    def test_page_flag_authoritative(self) -> None:
        meta = {
            "type": "auto-memory",
            "origin_scopes": [LEGIT_SCOPE],
            "ephemeral": True,
        }
        assert (
            classify_ephemeral_page(
                meta, "body", ephemeral_scopes=self._scopes(), operational_markers=[]
            )
            is not None
        )
