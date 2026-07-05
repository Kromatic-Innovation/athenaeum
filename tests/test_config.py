"""Tests for the athenaeum config module."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from athenaeum.config import (
    load_config,
    resolve_audience,
    resolve_extra_intake_roots,
    resolve_min_cluster_cohesion,
    resolve_min_cluster_cohesion_scopes,
    resolve_owner,
    resolve_page_flag_bytes,
    resolve_page_warn_bytes,
    resolve_push_after_run,
    resolve_push_branch,
    resolve_push_remote,
    resolve_retire,
    write_default_config,
)


class TestResolvePageWarnBytes:
    def test_default(self) -> None:
        assert resolve_page_warn_bytes(None) == 8192
        assert resolve_page_warn_bytes({}) == 8192
        assert resolve_page_warn_bytes({"librarian": {}}) == 8192

    def test_yaml_value_wins(self) -> None:
        assert resolve_page_warn_bytes({"librarian": {"page_warn_bytes": 4096}}) == 4096

    def test_quoted_yaml_coerced(self) -> None:
        assert (
            resolve_page_warn_bytes({"librarian": {"page_warn_bytes": "4096"}}) == 4096
        )

    def test_bool_and_bad_and_nonpositive_fall_through(self) -> None:
        assert resolve_page_warn_bytes({"librarian": {"page_warn_bytes": True}}) == 8192
        assert (
            resolve_page_warn_bytes({"librarian": {"page_warn_bytes": "abc"}}) == 8192
        )
        assert resolve_page_warn_bytes({"librarian": {"page_warn_bytes": 0}}) == 8192
        assert resolve_page_warn_bytes({"librarian": {"page_warn_bytes": -5}}) == 8192

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_PAGE_WARN_BYTES", "1234")
        assert resolve_page_warn_bytes({"librarian": {"page_warn_bytes": 4096}}) == 1234

    def test_bad_env_falls_through_to_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_PAGE_WARN_BYTES", "notint")
        assert resolve_page_warn_bytes({"librarian": {"page_warn_bytes": 4096}}) == 4096

    def test_not_seeded_in_defaults(self) -> None:
        from athenaeum.config import _DEFAULTS

        assert "page_warn_bytes" not in _DEFAULTS.get("librarian", {})


class TestResolvePageFlagBytes:
    def test_default(self) -> None:
        assert resolve_page_flag_bytes(None) == 16384
        assert resolve_page_flag_bytes({}) == 16384
        assert resolve_page_flag_bytes({"librarian": {}}) == 16384

    def test_yaml_value_wins(self) -> None:
        assert (
            resolve_page_flag_bytes({"librarian": {"page_flag_bytes": 30000}}) == 30000
        )

    def test_bool_and_bad_and_nonpositive_fall_through(self) -> None:
        assert (
            resolve_page_flag_bytes({"librarian": {"page_flag_bytes": True}}) == 16384
        )
        assert (
            resolve_page_flag_bytes({"librarian": {"page_flag_bytes": "abc"}}) == 16384
        )
        assert resolve_page_flag_bytes({"librarian": {"page_flag_bytes": 0}}) == 16384

    def test_env_wins_over_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_PAGE_FLAG_BYTES", "9999")
        assert (
            resolve_page_flag_bytes({"librarian": {"page_flag_bytes": 30000}}) == 9999
        )

    def test_not_seeded_in_defaults(self) -> None:
        from athenaeum.config import _DEFAULTS

        assert "page_flag_bytes" not in _DEFAULTS.get("librarian", {})


class TestResolveAudience:
    """Serve-time read-scope resolution, CLI > env > yaml > None (issue #312)."""

    def test_default_is_none_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_AUDIENCE", raising=False)
        assert resolve_audience(None) is None
        assert resolve_audience({}) is None
        assert resolve_audience({"serve": {}}) is None

    def test_yaml_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_AUDIENCE", raising=False)
        assert resolve_audience(
            {"serve": {"audience": ["Operations", "voltaire"]}}
        ) == {
            "operations",
            "voltaire",
        }

    def test_yaml_comma_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_AUDIENCE", raising=False)
        assert resolve_audience({"serve": {"audience": "ops, voltaire"}}) == {
            "ops",
            "voltaire",
        }

    def test_env_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_AUDIENCE", "marketing")
        assert resolve_audience({"serve": {"audience": ["operations"]}}) == {
            "marketing"
        }

    def test_cli_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_AUDIENCE", "marketing")
        assert resolve_audience({}, "operations,voltaire") == {
            "operations",
            "voltaire",
        }

    def test_empty_value_is_owner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An explicitly empty pin at any tier resolves to owner (full access).
        monkeypatch.setenv("ATHENAEUM_AUDIENCE", "  ")
        assert resolve_audience({"serve": {"audience": ["operations"]}}) is None
        monkeypatch.delenv("ATHENAEUM_AUDIENCE", raising=False)
        assert resolve_audience({}, "") is None
        assert resolve_audience({"serve": {"audience": []}}) is None

    def test_not_seeded_in_defaults(self) -> None:
        from athenaeum.config import _DEFAULTS

        assert "serve" not in _DEFAULTS


class TestResolveMinClusterCohesion:
    def test_default_off(self) -> None:
        # Default 0.0 (off): unset config / missing key never suppresses.
        assert resolve_min_cluster_cohesion(None) == 0.0
        assert resolve_min_cluster_cohesion({}) == 0.0
        assert resolve_min_cluster_cohesion({"librarian": {}}) == 0.0

    def test_yaml_value_wins(self) -> None:
        assert resolve_min_cluster_cohesion(
            {"librarian": {"min_cluster_cohesion": 0.47}}
        ) == pytest.approx(0.47)

    def test_bool_falls_through_to_off(self) -> None:
        # bool is an int subclass; `min_cluster_cohesion: true` must not become 1.0.
        cfg = {"librarian": {"min_cluster_cohesion": True}}
        assert resolve_min_cluster_cohesion(cfg) == 0.0

    def test_non_numeric_and_negative_fall_through(self) -> None:
        assert (
            resolve_min_cluster_cohesion({"librarian": {"min_cluster_cohesion": "x"}})
            == 0.0
        )
        assert (
            resolve_min_cluster_cohesion({"librarian": {"min_cluster_cohesion": -0.5}})
            == 0.0
        )

    def test_not_seeded_in_defaults(self) -> None:
        from athenaeum.config import _DEFAULTS

        assert "min_cluster_cohesion" not in _DEFAULTS.get("librarian", {})


class TestResolveMinClusterCohesionScopes:
    def test_default_is_four(self) -> None:
        assert resolve_min_cluster_cohesion_scopes(None) == 4
        assert resolve_min_cluster_cohesion_scopes({"librarian": {}}) == 4

    def test_yaml_value_wins(self) -> None:
        assert (
            resolve_min_cluster_cohesion_scopes(
                {"librarian": {"min_cluster_cohesion_scopes": 6}}
            )
            == 6
        )

    def test_below_two_and_bool_fall_through(self) -> None:
        assert (
            resolve_min_cluster_cohesion_scopes(
                {"librarian": {"min_cluster_cohesion_scopes": 1}}
            )
            == 4
        )
        assert (
            resolve_min_cluster_cohesion_scopes(
                {"librarian": {"min_cluster_cohesion_scopes": True}}
            )
            == 4
        )
        assert (
            resolve_min_cluster_cohesion_scopes(
                {"librarian": {"min_cluster_cohesion_scopes": "x"}}
            )
            == 4
        )


class TestResolveRetire:
    def test_default_on(self) -> None:
        # Default ON (owner-confirmed): unset config / missing key stays on.
        assert resolve_retire(None) is True
        assert resolve_retire({}) is True
        assert resolve_retire({"librarian": {}}) is True

    def test_yaml_false_disables(self) -> None:
        assert resolve_retire({"librarian": {"retire": False}}) is False

    def test_yaml_true_enables(self) -> None:
        assert resolve_retire({"librarian": {"retire": True}}) is True

    def test_non_bool_falls_through_to_default(self) -> None:
        # A non-bool (e.g. yaml string) must not silently disable retire.
        assert resolve_retire({"librarian": {"retire": "no"}}) is True

    def test_not_seeded_in_defaults(self) -> None:
        from athenaeum.config import _DEFAULTS

        assert "librarian" not in _DEFAULTS


class TestResolvePushAfterRun:
    def test_default_off(self) -> None:
        # Default OFF: a fresh install must never push without explicit opt-in.
        assert resolve_push_after_run(None) is False
        assert resolve_push_after_run({}) is False
        assert resolve_push_after_run({"librarian": {}}) is False

    def test_yaml_true_enables(self) -> None:
        assert resolve_push_after_run({"librarian": {"push_after_run": True}}) is True

    def test_yaml_false_explicit(self) -> None:
        assert resolve_push_after_run({"librarian": {"push_after_run": False}}) is False

    def test_non_bool_falls_through_to_off(self) -> None:
        # A non-bool (e.g. yaml string) must not silently enable push.
        assert resolve_push_after_run({"librarian": {"push_after_run": "yes"}}) is False

    def test_not_seeded_in_defaults(self) -> None:
        from athenaeum.config import _DEFAULTS

        assert "librarian" not in _DEFAULTS


class TestResolvePushRemote:
    def test_default_is_origin(self) -> None:
        assert resolve_push_remote(None) == "origin"
        assert resolve_push_remote({}) == "origin"
        assert resolve_push_remote({"librarian": {}}) == "origin"

    def test_yaml_value_wins(self) -> None:
        assert resolve_push_remote({"librarian": {"push_remote": "backup"}}) == "backup"

    def test_blank_and_non_string_fall_through(self) -> None:
        assert resolve_push_remote({"librarian": {"push_remote": ""}}) == "origin"
        assert resolve_push_remote({"librarian": {"push_remote": "   "}}) == "origin"
        assert resolve_push_remote({"librarian": {"push_remote": 42}}) == "origin"


class TestResolvePushBranch:
    def test_default_is_none(self) -> None:
        # ``None`` means "let `git push` use the configured upstream for the
        # current branch" — the conventional nightly setup.
        assert resolve_push_branch(None) is None
        assert resolve_push_branch({}) is None
        assert resolve_push_branch({"librarian": {}}) is None

    def test_yaml_value_wins(self) -> None:
        assert (
            resolve_push_branch({"librarian": {"push_branch": "develop"}}) == "develop"
        )

    def test_blank_and_non_string_fall_through(self) -> None:
        assert resolve_push_branch({"librarian": {"push_branch": ""}}) is None
        assert resolve_push_branch({"librarian": {"push_branch": 1}}) is None


class TestResolveOwner:
    def test_none_config_is_inert(self) -> None:
        assert resolve_owner(None) is None

    def test_missing_owner_block_is_inert(self) -> None:
        assert resolve_owner({"search_backend": "fts5"}) is None

    def test_empty_owner_block_is_inert(self) -> None:
        assert resolve_owner({"owner": {}}) is None
        assert resolve_owner({"owner": {"uid": "", "aliases": []}}) is None

    def test_non_dict_owner_is_inert(self) -> None:
        assert resolve_owner({"owner": "a545c038"}) is None

    def test_full_owner_block(self) -> None:
        owner = resolve_owner(
            {
                "owner": {
                    "uid": "a545c038",
                    "google_contact": "people/c765728850212863135",
                    "aliases": ["user_tristan", "Tristan Kromer"],
                }
            }
        )
        assert owner == {
            "uid": "a545c038",
            "google_contact": "people/c765728850212863135",
            "aliases": ["user_tristan", "Tristan Kromer"],
        }

    def test_partial_owner_uid_only(self) -> None:
        assert resolve_owner({"owner": {"uid": "a545c038"}}) == {
            "uid": "a545c038",
            "google_contact": "",
            "aliases": [],
        }

    def test_aliases_coerced_and_blanks_dropped(self) -> None:
        owner = resolve_owner({"owner": {"aliases": ["  user_x  ", "", None, 7]}})
        assert owner is not None
        assert owner["aliases"] == ["user_x", "7"]

    def test_template_documents_owner(self) -> None:
        from athenaeum.config import _DEFAULT_CONFIG_CONTENT

        assert "owner:" in _DEFAULT_CONFIG_CONTENT
        assert "google_contact" in _DEFAULT_CONFIG_CONTENT

    def test_owner_not_seeded_in_defaults(self) -> None:
        from athenaeum.config import _DEFAULTS

        assert "owner" not in _DEFAULTS
        # Unset owner stays inert through the full load path.
        assert resolve_owner(load_config(Path("/nonexistent-knowledge-root"))) is None


class TestLoadConfig:
    def test_defaults_when_no_file(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is True
        assert cfg["search_backend"] == "fts5"
        assert cfg["vector"]["provider"] == "chromadb"

    def test_reads_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text(
            "auto_recall: false\nsearch_backend: vector\n"
        )
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is False
        assert cfg["search_backend"] == "vector"

    def test_partial_override(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("search_backend: vector\n")
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is True  # default preserved
        assert cfg["search_backend"] == "vector"

    def test_vector_nested_merge(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("vector:\n  provider: faiss\n")
        cfg = load_config(tmp_path)
        assert cfg["vector"]["provider"] == "faiss"
        assert cfg["vector"]["collection"] == "wiki"  # default preserved

    def test_invalid_yaml_falls_back(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("{{invalid yaml")
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is True  # defaults

    def test_empty_file_falls_back(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("")
        cfg = load_config(tmp_path)
        assert cfg["auto_recall"] is True

    def test_mutating_returned_config_does_not_leak(self, tmp_path: Path) -> None:
        """A shallow seed copy aliased _DEFAULTS' nested values: mutating
        ``recall.extra_intake_roots`` on one returned config corrupted every
        subsequent load_config() process-wide. The seed must be a deep copy.
        """
        cfg = load_config(tmp_path)
        cfg["recall"]["extra_intake_roots"].append("raw/mutated")
        cfg["vector"]["provider"] = "mutated"

        fresh = load_config(tmp_path)
        assert fresh["recall"]["extra_intake_roots"] == ["raw/auto-memory"]
        assert fresh["vector"]["provider"] == "chromadb"


class TestDefaultsDoNotShadowCodeDefaults:
    """Regression tests for issue #231.

    ``_DEFAULTS`` used to seed concrete values for keys whose real
    defaults live next to their consumer code (``contradiction.*``,
    ``librarian.cluster_threshold`` / ``cluster_output``). Because
    ``load_config()`` always merged those seeds in, every resolver saw
    them as "user-set" and its own code default became unreachable —
    which is how the #187 resolver-cap raise (50 -> 250) was silently
    reverted to 50 through the config path.
    """

    def test_resolver_cap_default_reaches_187_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE #231 bug: a plain load_config() must yield the #187 cap."""
        monkeypatch.delenv("ATHENAEUM_RESOLVE_MAX_PER_RUN", raising=False)
        from athenaeum.resolutions import (
            DEFAULT_RESOLVE_MAX_PER_RUN,
            resolve_max_per_run,
        )

        cfg = load_config(tmp_path)
        assert DEFAULT_RESOLVE_MAX_PER_RUN == 250
        assert resolve_max_per_run(cfg) == DEFAULT_RESOLVE_MAX_PER_RUN

    def test_cross_scope_mode_code_default_reachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_CROSS_SCOPE_MODE", raising=False)
        from athenaeum.cross_scope import DEFAULT_MODE, resolve_cross_scope_mode

        cfg = load_config(tmp_path)
        assert "cross_scope_mode" not in (cfg.get("contradiction") or {})
        assert resolve_cross_scope_mode(cfg) == DEFAULT_MODE

    def test_resolved_similarity_threshold_code_default_reachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD", raising=False)
        from athenaeum.fingerprint import (
            _DEFAULT_RESOLVED_SIMILARITY_THRESHOLD,
            resolve_resolved_similarity_threshold,
        )

        cfg = load_config(tmp_path)
        assert "resolved_similarity_threshold" not in (cfg.get("contradiction") or {})
        assert resolve_resolved_similarity_threshold(cfg) == pytest.approx(
            _DEFAULT_RESOLVED_SIMILARITY_THRESHOLD
        )

    def test_cluster_size_cap_code_default_reachable(self, tmp_path: Path) -> None:
        from athenaeum.cross_scope import (
            DEFAULT_CLUSTER_SIZE_CAP,
            resolve_cluster_size_cap,
        )

        cfg = load_config(tmp_path)
        assert "cluster_size_cap" not in (cfg.get("contradiction") or {})
        assert DEFAULT_CLUSTER_SIZE_CAP == 25
        assert resolve_cluster_size_cap(cfg) == DEFAULT_CLUSTER_SIZE_CAP

    def test_similarity_threshold_code_default_reachable(self, tmp_path: Path) -> None:
        from athenaeum.cross_scope import (
            DEFAULT_SIMILARITY_THRESHOLD,
            resolve_similarity_threshold,
        )

        cfg = load_config(tmp_path)
        assert "similarity_threshold" not in (cfg.get("contradiction") or {})
        assert DEFAULT_SIMILARITY_THRESHOLD == pytest.approx(0.85)
        assert resolve_similarity_threshold(cfg) == pytest.approx(
            DEFAULT_SIMILARITY_THRESHOLD
        )

    def test_cluster_output_code_default_reachable(self, tmp_path: Path) -> None:
        from athenaeum.clusters import (
            DEFAULT_CLUSTER_OUTPUT,
            resolve_cluster_output_path,
        )

        cfg = load_config(tmp_path)
        assert "cluster_output" not in (cfg.get("librarian") or {})
        assert (
            resolve_cluster_output_path(tmp_path, config=cfg)
            == tmp_path / DEFAULT_CLUSTER_OUTPUT
        )

    def test_cluster_threshold_code_default_reachable(self, tmp_path: Path) -> None:
        from athenaeum.clusters import (
            DEFAULT_CLUSTER_THRESHOLD,
            resolve_cluster_threshold,
        )

        cfg = load_config(tmp_path)
        assert "cluster_threshold" not in (cfg.get("librarian") or {})
        assert resolve_cluster_threshold(tmp_path, config=cfg) == pytest.approx(
            DEFAULT_CLUSTER_THRESHOLD
        )

    def test_user_yaml_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit yaml values must survive load_config and beat code defaults."""
        monkeypatch.delenv("ATHENAEUM_RESOLVE_MAX_PER_RUN", raising=False)
        monkeypatch.delenv("ATHENAEUM_CROSS_SCOPE_MODE", raising=False)
        monkeypatch.delenv("ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD", raising=False)
        from athenaeum.clusters import resolve_cluster_threshold
        from athenaeum.cross_scope import resolve_cross_scope_mode
        from athenaeum.fingerprint import resolve_resolved_similarity_threshold
        from athenaeum.resolutions import resolve_max_per_run

        (tmp_path / "athenaeum.yaml").write_text(
            "contradiction:\n"
            "  resolve_max_per_run: 7\n"
            "  cross_scope_mode: similarity\n"
            "  resolved_similarity_threshold: 0.9\n"
            "librarian:\n"
            "  cluster_threshold: 0.75\n"
        )
        cfg = load_config(tmp_path)
        assert resolve_max_per_run(cfg) == 7
        assert resolve_cross_scope_mode(cfg) == "similarity"
        assert resolve_resolved_similarity_threshold(cfg) == pytest.approx(0.9)
        assert resolve_cluster_threshold(tmp_path, config=cfg) == pytest.approx(0.75)

    def test_unknown_top_level_sections_pass_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sections absent from _DEFAULTS (e.g. ``resolve:``) must not be dropped."""
        monkeypatch.delenv("ATHENAEUM_RESOLVE_MODEL", raising=False)
        from athenaeum.resolutions import _get_model

        (tmp_path / "athenaeum.yaml").write_text(
            "resolve:\n  model: claude-haiku-test\n"
        )
        cfg = load_config(tmp_path)
        assert cfg["resolve"]["model"] == "claude-haiku-test"
        # End-to-end: the resolver must actually see the user-set model.
        assert _get_model(cfg) == "claude-haiku-test"

    def test_template_advertises_187_cap(self) -> None:
        from athenaeum.config import _DEFAULT_CONFIG_CONTENT

        assert "resolve_max_per_run: 250" in _DEFAULT_CONFIG_CONTENT
        assert "resolve_max_per_run: 50" not in _DEFAULT_CONFIG_CONTENT

    def test_template_names_live_resolver_model_key(self) -> None:
        """The template must advertise ``resolve.model`` (the key _get_model
        reads), not the dead ``contradiction.resolve_model``.
        """
        from athenaeum.config import _DEFAULT_CONFIG_CONTENT

        assert "resolve_model:" not in _DEFAULT_CONFIG_CONTENT
        assert "# resolve:" in _DEFAULT_CONFIG_CONTENT
        assert "#   model: claude-opus-4-7" in _DEFAULT_CONFIG_CONTENT


class TestNonDictSectionsDegradeGracefully:
    """A truthy scalar/list section value (``contradiction: oops``) must
    degrade to code defaults, not crash the resolver functions.
    """

    @pytest.mark.parametrize(
        "bad_value", ["oops", ["a", "b"], 3, 1.5], ids=["str", "list", "int", "float"]
    )
    def test_scalar_or_list_sections_fall_back_to_code_defaults(
        self, bad_value: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATHENAEUM_CROSS_SCOPE_MODE", raising=False)
        monkeypatch.delenv("ATHENAEUM_RESOLVED_SIMILARITY_THRESHOLD", raising=False)
        from athenaeum.clusters import (
            DEFAULT_CLUSTER_OUTPUT,
            DEFAULT_CLUSTER_THRESHOLD,
            resolve_cluster_output_path,
            resolve_cluster_threshold,
        )
        from athenaeum.cross_scope import (
            DEFAULT_CLUSTER_SIZE_CAP,
            DEFAULT_MODE,
            DEFAULT_SIMILARITY_THRESHOLD,
            resolve_cluster_size_cap,
            resolve_cross_scope_mode,
            resolve_similarity_threshold,
        )
        from athenaeum.fingerprint import (
            _DEFAULT_RESOLVED_SIMILARITY_THRESHOLD,
            resolve_resolved_similarity_threshold,
        )

        cfg = {"contradiction": bad_value, "librarian": bad_value}
        assert resolve_cross_scope_mode(cfg) == DEFAULT_MODE
        assert resolve_resolved_similarity_threshold(cfg) == pytest.approx(
            _DEFAULT_RESOLVED_SIMILARITY_THRESHOLD
        )
        assert resolve_cluster_size_cap(cfg) == DEFAULT_CLUSTER_SIZE_CAP
        assert resolve_similarity_threshold(cfg) == pytest.approx(
            DEFAULT_SIMILARITY_THRESHOLD
        )
        assert resolve_cluster_threshold(tmp_path, config=cfg) == pytest.approx(
            DEFAULT_CLUSTER_THRESHOLD
        )
        assert (
            resolve_cluster_output_path(tmp_path, config=cfg)
            == tmp_path / DEFAULT_CLUSTER_OUTPUT
        )


class TestRecallExtraIntakeRootsDefault:
    """The default config advertises ``raw/auto-memory`` as an extra root
    so agent-written memories participate in recall without ceremony.
    """

    def test_default_includes_auto_memory(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path)
        assert cfg["recall"]["extra_intake_roots"] == ["raw/auto-memory"]

    def test_user_override_replaces_list(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("recall:\n  extra_intake_roots: []\n")
        cfg = load_config(tmp_path)
        assert cfg["recall"]["extra_intake_roots"] == []


class TestResolveExtraIntakeRoots:
    def test_resolves_relative_path(self, tmp_path: Path) -> None:
        (tmp_path / "raw" / "auto-memory").mkdir(parents=True)
        resolved = resolve_extra_intake_roots(tmp_path)
        assert len(resolved) == 1
        assert resolved[0].name == "auto-memory"
        assert resolved[0].is_absolute()

    def test_drops_missing_roots(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing intake roots must not blow up index build, but they
        should emit a WARNING so operators notice a typo'd or unmounted
        path rather than silently losing recall coverage.
        """
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            resolved = resolve_extra_intake_roots(tmp_path)
        assert resolved == []
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "extra_intake_root not found" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "raw/auto-memory" in warnings[0].getMessage()

    def test_accepts_absolute_path(self, tmp_path: Path) -> None:
        extra = tmp_path / "extra"
        extra.mkdir()
        (tmp_path / "athenaeum.yaml").write_text(
            f"recall:\n  extra_intake_roots:\n    - {extra}\n"
        )
        resolved = resolve_extra_intake_roots(tmp_path)
        assert resolved == [extra.resolve()]

    def test_empty_list_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "athenaeum.yaml").write_text("recall:\n  extra_intake_roots: []\n")
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            resolved = resolve_extra_intake_roots(tmp_path)
        assert resolved == []
        # Empty list must stay silent — no dropped paths to warn about.
        assert not [
            r for r in caplog.records if "extra_intake_root not found" in r.getMessage()
        ]

    def test_non_list_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed config (scalar instead of list) degrades gracefully."""
        (tmp_path / "athenaeum.yaml").write_text(
            "recall:\n  extra_intake_roots: raw/auto-memory\n"
        )
        with caplog.at_level(logging.WARNING, logger="athenaeum.config"):
            resolved = resolve_extra_intake_roots(tmp_path)
        assert resolved == []
        # Non-list config is a distinct failure mode (malformed yaml),
        # not per-root warnings — stay silent here too.
        assert not [
            r for r in caplog.records if "extra_intake_root not found" in r.getMessage()
        ]


class TestWriteDefaultConfig:
    def test_creates_file(self, tmp_path: Path) -> None:
        path = write_default_config(tmp_path)
        assert path.exists()
        content = path.read_text()
        assert "auto_recall" in content
        assert "search_backend" in content

    def test_idempotent(self, tmp_path: Path) -> None:
        (tmp_path / "athenaeum.yaml").write_text("custom: true\n")
        write_default_config(tmp_path)
        assert "custom: true" in (tmp_path / "athenaeum.yaml").read_text()
