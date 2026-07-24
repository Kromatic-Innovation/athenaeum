# SPDX-License-Identifier: Apache-2.0
"""Tests for the pluggable storage-surface layer (issue #429)."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum import storage
from athenaeum.config import resolve_storage_adapters, resolve_storage_mapping
from athenaeum.storage import (
    DEFAULT_ADAPTER_NAME,
    CorpusPolicy,
    StorageAdapter,
    StorageConfigError,
    available_adapters,
    corpus_policy_for_class,
    is_embedded,
    is_excluded,
    is_merge_eligible,
    is_recallable,
    register_adapter,
    resolve_adapter_for_class,
    surface_root_for_class,
)


@pytest.fixture(autouse=True)
def _isolate_registered_adapters() -> None:
    """Snapshot/restore the in-process adapter registry between tests."""
    snapshot = dict(storage._REGISTERED_ADAPTERS)
    try:
        yield
    finally:
        storage._REGISTERED_ADAPTERS.clear()
        storage._REGISTERED_ADAPTERS.update(snapshot)


# ---------------------------------------------------------------------------
# CorpusPolicy
# ---------------------------------------------------------------------------


class TestCorpusPolicy:
    def test_all_is_full_participation(self) -> None:
        p = CorpusPolicy.all()
        assert (p.embedded, p.recallable, p.merge_eligible) == (True, True, True)
        assert p.in_corpus is True

    def test_none_is_no_participation(self) -> None:
        p = CorpusPolicy.none()
        assert (p.embedded, p.recallable, p.merge_eligible) == (False, False, False)
        assert p.in_corpus is False

    def test_in_corpus_true_when_any_capability_set(self) -> None:
        assert CorpusPolicy(embedded=False, recallable=True, merge_eligible=False).in_corpus
        assert CorpusPolicy(embedded=True, recallable=False, merge_eligible=False).in_corpus
        assert not CorpusPolicy(False, False, False).in_corpus

    def test_frozen(self) -> None:
        with pytest.raises(Exception):
            CorpusPolicy.all().embedded = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# StorageAdapter.resolve_root
# ---------------------------------------------------------------------------


class TestResolveRoot:
    def test_relative_root_joined_under_knowledge_root(self, tmp_path: Path) -> None:
        adapter = StorageAdapter("x", "markdown", "wiki", CorpusPolicy.all())
        assert adapter.resolve_root(tmp_path) == tmp_path / "wiki"

    def test_absolute_root_honored_verbatim(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "somewhere" / "else"
        adapter = StorageAdapter("x", "markdown", str(elsewhere), CorpusPolicy.none())
        assert adapter.resolve_root(tmp_path / "kr") == elsewhere


# ---------------------------------------------------------------------------
# Default (unconfigured) resolution — byte-identical guarantees
# ---------------------------------------------------------------------------


class TestDefaultResolution:
    @pytest.mark.parametrize("cls", ["person", "concept", "reference", "totally-new"])
    def test_every_class_defaults_to_wiki_surface(self, cls: str) -> None:
        adapter = resolve_adapter_for_class(cls, None)
        assert adapter.name == DEFAULT_ADAPTER_NAME
        assert adapter.surface_root == "wiki"
        assert adapter.corpus_policy == CorpusPolicy.all()

    @pytest.mark.parametrize("cls", [None, "", "   "])
    def test_none_or_blank_class_defaults_to_wiki(self, cls: str | None) -> None:
        assert resolve_adapter_for_class(cls, None).name == DEFAULT_ADAPTER_NAME

    def test_predicates_all_true_by_default(self) -> None:
        assert is_embedded("person", None)
        assert is_recallable("person", None)
        assert is_merge_eligible("person", None)
        assert not is_excluded("person", None)

    def test_builtins_always_available_without_config(self) -> None:
        adapters = available_adapters(None)
        assert set(adapters) == {DEFAULT_ADAPTER_NAME, "excluded"}
        assert adapters["excluded"].corpus_policy == CorpusPolicy.none()


# ---------------------------------------------------------------------------
# Config-driven mapping to the built-in excluded adapter (#427's consumer)
# ---------------------------------------------------------------------------


class TestMappingToExcluded:
    def test_class_mapped_to_excluded_is_out_of_corpus(self) -> None:
        config = {"storage": {"mapping": {"pii": "excluded"}}}
        assert resolve_adapter_for_class("pii", config).name == "excluded"
        assert not is_embedded("pii", config)
        assert not is_recallable("pii", config)
        assert not is_merge_eligible("pii", config)
        assert is_excluded("pii", config)

    def test_unmapped_class_still_defaults_to_wiki(self) -> None:
        config = {"storage": {"mapping": {"pii": "excluded"}}}
        assert resolve_adapter_for_class("person", config).name == DEFAULT_ADAPTER_NAME
        assert is_merge_eligible("person", config)

    def test_surface_root_for_excluded_is_outside_wiki(self, tmp_path: Path) -> None:
        config = {"storage": {"mapping": {"pii": "excluded"}}}
        root = surface_root_for_class("pii", config, tmp_path)
        assert root == tmp_path / "excluded"
        assert (tmp_path / "wiki") not in root.parents and root != tmp_path / "wiki"


# ---------------------------------------------------------------------------
# Custom adapters from config
# ---------------------------------------------------------------------------


class TestCustomConfigAdapters:
    def _config(self) -> dict:
        return {
            "storage": {
                "adapters": {
                    "contacts-excluded": {
                        "backing_store": "markdown",
                        "surface_root": "contacts",
                        "corpus_policy": {
                            "embedded": False,
                            "recallable": False,
                            "merge_eligible": False,
                        },
                    }
                },
                "mapping": {"pii": "contacts-excluded"},
            }
        }

    def test_custom_adapter_resolves(self, tmp_path: Path) -> None:
        config = self._config()
        adapter = resolve_adapter_for_class("pii", config)
        assert adapter.name == "contacts-excluded"
        assert adapter.backing_store == "markdown"
        assert adapter.resolve_root(tmp_path) == tmp_path / "contacts"
        assert is_excluded("pii", config)

    def test_partial_policy_fails_closed(self) -> None:
        # Only `embedded: true` specified — the other two default to False.
        config = {
            "storage": {
                "adapters": {
                    "half": {
                        "backing_store": "markdown",
                        "surface_root": "half",
                        "corpus_policy": {"embedded": True},
                    }
                },
                "mapping": {"weird": "half"},
            }
        }
        policy = corpus_policy_for_class("weird", config)
        assert policy.embedded is True
        assert policy.recallable is False
        assert policy.merge_eligible is False

    def test_omitted_policy_block_fails_closed(self) -> None:
        config = {
            "storage": {
                "adapters": {
                    "nostore": {"backing_store": "markdown", "surface_root": "nostore"}
                },
                "mapping": {"x": "nostore"},
            }
        }
        assert corpus_policy_for_class("x", config) == CorpusPolicy.none()

    def test_string_bools_coerced(self) -> None:
        config = {
            "storage": {
                "adapters": {
                    "s": {
                        "backing_store": "markdown",
                        "surface_root": "s",
                        "corpus_policy": {
                            "embedded": "true",
                            "recallable": "false",
                            "merge_eligible": "TRUE",
                        },
                    }
                },
                "mapping": {"y": "s"},
            }
        }
        p = corpus_policy_for_class("y", config)
        assert (p.embedded, p.recallable, p.merge_eligible) == (True, False, True)

    def test_non_bool_policy_value_fails_closed(self) -> None:
        config = {
            "storage": {
                "adapters": {
                    "s": {
                        "backing_store": "markdown",
                        "surface_root": "s",
                        "corpus_policy": {"embedded": 1, "recallable": "yes"},
                    }
                },
                "mapping": {"y": "s"},
            }
        }
        # 1 and "yes" are NOT accepted as truthy — fail closed.
        assert corpus_policy_for_class("y", config) == CorpusPolicy.none()


# ---------------------------------------------------------------------------
# Loud errors — misconfiguration must never silently fall back
# ---------------------------------------------------------------------------


class TestLoudErrors:
    def test_unknown_adapter_in_mapping_raises(self) -> None:
        config = {"storage": {"mapping": {"pii": "does-not-exist"}}}
        with pytest.raises(StorageConfigError, match="unknown adapter"):
            resolve_adapter_for_class("pii", config)

    def test_config_adapter_shadowing_builtin_raises(self) -> None:
        config = {
            "storage": {
                "adapters": {
                    DEFAULT_ADAPTER_NAME: {
                        "backing_store": "x",
                        "surface_root": "y",
                    }
                }
            }
        }
        with pytest.raises(StorageConfigError, match="shadows a built-in"):
            available_adapters(config)

    def test_missing_backing_store_raises(self) -> None:
        config = {
            "storage": {
                "adapters": {"bad": {"surface_root": "x"}},
                "mapping": {"z": "bad"},
            }
        }
        with pytest.raises(StorageConfigError, match="backing_store"):
            available_adapters(config)

    def test_missing_surface_root_raises(self) -> None:
        config = {
            "storage": {
                "adapters": {"bad": {"backing_store": "markdown"}},
                "mapping": {"z": "bad"},
            }
        }
        with pytest.raises(StorageConfigError, match="surface_root"):
            available_adapters(config)


# ---------------------------------------------------------------------------
# register_adapter — the in-process extension point
# ---------------------------------------------------------------------------


class TestRegisterAdapter:
    def test_registered_adapter_is_resolvable(self) -> None:
        register_adapter(
            StorageAdapter("skill-sync", "sqlite", "skills", CorpusPolicy.none())
        )
        config = {"storage": {"mapping": {"skill": "skill-sync"}}}
        adapter = resolve_adapter_for_class("skill", config)
        assert adapter.name == "skill-sync"
        assert adapter.backing_store == "sqlite"

    def test_cannot_shadow_builtin(self) -> None:
        with pytest.raises(StorageConfigError, match="shadows a built-in"):
            register_adapter(
                StorageAdapter(DEFAULT_ADAPTER_NAME, "x", "y", CorpusPolicy.all())
            )

    def test_duplicate_registration_raises_without_replace(self) -> None:
        register_adapter(StorageAdapter("dup", "x", "y", CorpusPolicy.none()))
        with pytest.raises(StorageConfigError, match="already registered"):
            register_adapter(StorageAdapter("dup", "x", "z", CorpusPolicy.none()))

    def test_replace_true_overrides(self) -> None:
        register_adapter(StorageAdapter("dup", "x", "y", CorpusPolicy.none()))
        register_adapter(
            StorageAdapter("dup", "x", "z", CorpusPolicy.all()), replace=True
        )
        assert available_adapters(None)["dup"].surface_root == "z"

    def test_config_adapter_overrides_registered(self) -> None:
        register_adapter(StorageAdapter("both", "code", "code-root", CorpusPolicy.none()))
        config = {
            "storage": {
                "adapters": {
                    "both": {"backing_store": "yaml", "surface_root": "yaml-root"}
                }
            }
        }
        # config-defined definition wins over the code-registered one.
        assert available_adapters(config)["both"].backing_store == "yaml"


# ---------------------------------------------------------------------------
# Config resolvers (defensive parsing)
# ---------------------------------------------------------------------------


class TestConfigResolvers:
    def test_mapping_empty_by_default(self) -> None:
        assert resolve_storage_mapping(None) == {}
        assert resolve_storage_mapping({}) == {}
        assert resolve_storage_mapping({"storage": {}}) == {}

    def test_mapping_drops_non_string_entries(self) -> None:
        config = {
            "storage": {
                "mapping": {"good": "excluded", "": "x", "blank": "  ", 3: "n"}
            }
        }
        assert resolve_storage_mapping(config) == {"good": "excluded"}

    def test_adapters_empty_by_default(self) -> None:
        assert resolve_storage_adapters(None) == {}
        assert resolve_storage_adapters({"storage": {"adapters": "notadict"}}) == {}

    def test_adapters_drops_malformed_entries(self) -> None:
        config = {
            "storage": {
                "adapters": {
                    "ok": {"backing_store": "m", "surface_root": "r"},
                    "": {"backing_store": "m", "surface_root": "r"},
                    "notdict": "nope",
                }
            }
        }
        assert set(resolve_storage_adapters(config)) == {"ok"}

    def test_storage_not_seeded_in_defaults(self) -> None:
        # Issue #231: the code default (empty) must stay reachable — no seed.
        from athenaeum.config import _DEFAULTS

        assert "storage" not in _DEFAULTS


# ---------------------------------------------------------------------------
# Functional integration: merge-eligibility gate honors the policy (#429)
# ---------------------------------------------------------------------------


def _write_page(wiki_root: Path, filename: str, *, page_type: str, body: str) -> None:
    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / filename).write_text(
        f"---\nname: {filename[:-3]}\ntype: {page_type}\n---\n{body}\n",
        encoding="utf-8",
    )


class TestMergeGateIntegration:
    def test_excluded_class_dropped_from_merge_candidates(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "a.md", page_type="concept", body="a")
        _write_page(wiki_root, "b.md", page_type="reference", body="b")

        # Route the `reference` class to the excluded surface (no merge).
        config = {"storage": {"mapping": {"reference": "excluded"}}}
        names = {c.path.name for c in discover_wiki_dedupe_candidates(wiki_root, config=config)}
        assert names == {"a.md"}  # b.md dropped by policy

    def test_no_config_is_byte_identical(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "a.md", page_type="concept", body="a")
        _write_page(wiki_root, "b.md", page_type="reference", body="b")

        # config=None => no policy consult => both remain candidates.
        names = {c.path.name for c in discover_wiki_dedupe_candidates(wiki_root)}
        assert names == {"a.md", "b.md"}

    def test_wiki_mapped_class_unaffected(self, tmp_path: Path) -> None:
        from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

        wiki_root = tmp_path / "wiki"
        _write_page(wiki_root, "a.md", page_type="concept", body="a")
        _write_page(wiki_root, "b.md", page_type="reference", body="b")

        # A mapping that only touches an unrelated class leaves both candidates.
        config = {"storage": {"mapping": {"pii": "excluded"}}}
        names = {c.path.name for c in discover_wiki_dedupe_candidates(wiki_root, config=config)}
        assert names == {"a.md", "b.md"}


# ---------------------------------------------------------------------------
# By-construction exclusion: an excluded surface root is not scanned for
# embed/recall — the fail-closed mechanism #427 relies on (no core change).
# ---------------------------------------------------------------------------


class TestByConstructionExclusion:
    def test_excluded_surface_not_scanned_by_index_builder(self, tmp_path: Path) -> None:
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.search import _scan_all_entries

        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        _write_page(wiki_root, "public.md", page_type="concept", body="public")

        # An excluded-surface page written OUTSIDE wiki/ (where #427's PII lands).
        config = {"storage": {"mapping": {"pii": "excluded"}}}
        excluded_root = surface_root_for_class("pii", config, knowledge_root)
        excluded_root.mkdir(parents=True, exist_ok=True)
        (excluded_root / "alice-phone.md").write_text(
            "---\nname: alice\ntype: pii\n---\n+1-555-0100\n", encoding="utf-8"
        )

        extra_roots = resolve_extra_intake_roots(knowledge_root, config=config)
        scanned = {name for name, _ in _scan_all_entries(wiki_root, extra_roots)}

        assert "public.md" in scanned
        assert not any("alice-phone" in n for n in scanned)
        # And the excluded root is genuinely outside the wiki tree.
        assert wiki_root not in excluded_root.parents
