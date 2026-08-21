# SPDX-License-Identifier: Apache-2.0
"""R3 artifact-classification enumeration test (issue athenaeum#980, slice S5
of the whole-store adapter design lock, issue athenaeum#911).

Acceptance criteria this file proves (see the issue body / design note §5.2,
§9.2 slice S5 row):

* AC1 — every artifact declares exactly one of the four R3 classes.
* AC2 — every ``operational`` artifact declares exactly one scope
  (``store-durable`` or ``machine-local``).
* AC5 — the ``config`` class covers ``rules/``, ``templates/``, the authority
  manifest, and ``athenaeum.yaml``.
* AC6 — this file IS the enumeration: it walks
  :data:`athenaeum.store.ARTIFACT_REGISTRY` and asserts the invariants above,
  plus spot-checks a representative artifact from each R3 class/scope so a
  regression that emptied the registry (rather than merely leaving one entry
  malformed) is also caught.

AC1/AC2 are additionally enforced STRUCTURALLY by
:meth:`athenaeum.store.ArtifactDeclaration.__post_init__` — malformed
declarations cannot even be constructed, so importing
:mod:`athenaeum.store` at all is already a partial proof. This file is the
independent, human-readable check: it re-verifies the invariant without
relying on the enforcement it is nominally testing, and it is the one place
that would fail loudly if a future edit deleted rows instead of merely
mis-declaring one.
"""

from __future__ import annotations

from athenaeum.store import (
    ARTIFACT_REGISTRY,
    OPERATIONAL_SCOPES,
    PERSISTENCE_CLASSES,
    ArtifactDeclaration,
)


class TestArtifactRegistryEnumeration:
    def test_registry_is_non_empty(self) -> None:
        assert len(ARTIFACT_REGISTRY) > 0

    def test_every_entry_is_an_artifact_declaration(self) -> None:
        for entry in ARTIFACT_REGISTRY:
            assert isinstance(entry, ArtifactDeclaration)

    def test_every_artifact_declares_exactly_one_class(self) -> None:
        """AC1: every artifact in the registry names exactly one of the four
        R3 classes. ``persistence_class`` is a single required ``str`` field
        (not a set/list), so "more than one" is not representable — this
        test asserts the one value that IS present is a real R3 class."""
        for entry in ARTIFACT_REGISTRY:
            assert entry.persistence_class in PERSISTENCE_CLASSES, (
                f"{entry.name!r} declares {entry.persistence_class!r}, "
                f"not one of {sorted(PERSISTENCE_CLASSES)}"
            )

    def test_every_operational_artifact_declares_exactly_one_scope(self) -> None:
        """AC2: an ``operational`` artifact names exactly one of
        ``store-durable``/``machine-local``; a non-``operational`` artifact
        names none."""
        for entry in ARTIFACT_REGISTRY:
            if entry.persistence_class == "operational":
                assert entry.operational_scope in OPERATIONAL_SCOPES, (
                    f"{entry.name!r} is 'operational' but declares scope "
                    f"{entry.operational_scope!r}"
                )
            else:
                assert entry.operational_scope is None, (
                    f"{entry.name!r} is {entry.persistence_class!r} but "
                    f"declares a scope ({entry.operational_scope!r}); only "
                    "'operational' artifacts may"
                )

    def test_names_are_unique(self) -> None:
        names = [entry.name for entry in ARTIFACT_REGISTRY]
        assert len(names) == len(set(names)), "duplicate artifact name in ARTIFACT_REGISTRY"

    def test_post_init_rejects_operational_without_scope(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="operational_scope"):
            ArtifactDeclaration(
                name="bad",
                persistence_class="operational",
                operational_scope=None,
                location="wiki root",
                source_ref="test",
            )

    def test_post_init_rejects_scope_on_non_operational(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="must not declare"):
            ArtifactDeclaration(
                name="bad",
                persistence_class="source",
                operational_scope="store-durable",
                location="wiki root",
                source_ref="test",
            )

    def test_post_init_rejects_unknown_class(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="persistence_class"):
            ArtifactDeclaration(
                name="bad",
                persistence_class="not-a-real-class",
                operational_scope=None,
                location="wiki root",
                source_ref="test",
            )


class TestArtifactRegistryClassMembership:
    """Spot-checks tying specific, issue-named artifacts to their R3 class —
    catches a regression that leaves the registry non-empty but reclassifies
    (or drops) a specific artifact the acceptance criteria name."""

    @staticmethod
    def _by_name(name: str) -> ArtifactDeclaration:
        for entry in ARTIFACT_REGISTRY:
            if entry.name == name:
                return entry
        raise AssertionError(f"no ARTIFACT_REGISTRY entry named {name!r}")

    def test_raw_intake_and_wiki_pages_are_source(self) -> None:
        assert self._by_name("raw-intake").persistence_class == "source"
        assert self._by_name("compiled-wiki-pages").persistence_class == "source"

    def test_search_index_artifacts_are_derived(self) -> None:
        for name in (
            "fts5-index-db",
            "vector-collection",
            "fts5-manifest",
            "vector-manifest",
            "vector-generation-stamp",
            "ingest-manifest",
            "auto-memory-manifest",
        ):
            assert self._by_name(name).persistence_class == "derived"

    def test_quarantine_and_named_wiki_root_ledgers_are_operational_store_durable(self) -> None:
        for name in (
            "quarantine-ledger",
            "merge-provenance-ledger",
            "pending-retractions-ledger",
            "calibration-ledger",
            "reasoning-tier-decisions-ledger",
            "axiom-governance-ledger",
            "corrections-applied-ledger",
            "shape-rules-applied-ledger",
            "shape-rule-dispositions-ledger",
        ):
            entry = self._by_name(name)
            assert entry.persistence_class == "operational"
            assert entry.operational_scope == "store-durable"

    def test_machine_scoped_cache_state_is_operational_machine_local(self) -> None:
        """Design note §5.2 table row 9: detection/zero-yield/killswitch state
        is machine-scoped and must stay OUT of the seam (AC4's other half)."""
        for name in (
            "detection-incomplete-state",
            "zero-yield-state",
            "killswitch-state",
        ):
            entry = self._by_name(name)
            assert entry.persistence_class == "operational"
            assert entry.operational_scope == "machine-local"
            assert entry.location == "cache dir"

    def test_ac5_config_class_covers_exactly_the_named_four(self) -> None:
        """AC5: 'The config class covers rules/, templates/, the authority
        manifest and athenaeum.yaml.'"""
        config_entries = {e.name: e for e in ARTIFACT_REGISTRY if e.persistence_class == "config"}
        assert set(config_entries) == {
            "shape-rules",
            "entity-templates",
            "authority-manifest",
            "athenaeum-config",
        }

    def test_cache_dir_durable_ledgers_are_operational_store_durable(self) -> None:
        """The design note §5.2 table's cache-dir 'no' (not reconstructible)
        row: these are the artifacts AC4 requires split OUT of the cache dir
        by scope — every one of them is declared 'operational'/'store-durable'
        here (the classification half of AC4; see each entry's source_ref in
        athenaeum/store.py for what physically moved vs. what has a
        not-yet-wired relocation mechanism)."""
        for name in (
            "llm-schema-observations-ledger",
            "spend-ledger",
            "push-records-ledger",
        ):
            entry = self._by_name(name)
            assert entry.persistence_class == "operational"
            assert entry.operational_scope == "store-durable"

    def test_registry_json_and_compiled_exempt_are_operational_not_config(self) -> None:
        """Design note §2.3.1's wider sweep paragraph calls these
        'operator-authored', but R3's own box definition (§5.2) names only
        rules/templates/authority-manifest/athenaeum.yaml as 'config' — see
        each entry's source_ref for the resolution."""
        for name in ("corrections-entity-registry", "compiled-exempt-manifest"):
            entry = self._by_name(name)
            assert entry.persistence_class == "operational"
            assert entry.operational_scope == "store-durable"

    def test_preserved_log_area_is_source_not_config(self) -> None:
        """The preserved-log directory HOLDS preserved raw content (a
        retained source document, design note §5.1) — it is not itself an
        operator-authored behavioural declaration."""
        entry = self._by_name("preserved-log-area")
        assert entry.persistence_class == "source"
