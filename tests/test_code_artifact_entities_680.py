"""Code artifacts must not become wiki entities (issue athenaeum#680).

The librarian was minting durable wiki entities from source-code artifacts
(``skill.md``, ``project-registry.yaml``, ``registry`` -> ``auto-registry.md``).
A wiki page describing a file's PAST state is actively harmful: an agent recalls
it, treats it as current, and spends a session disproving it against the working
tree. This is a WRITE-side class exclusion applied at entity creation — filenames
are an unbounded set a stopword list (athenaeum#662) cannot enumerate.

The companion one-shot sweep that used to retire already-on-disk
filename-derived pages (``athenaeum.filename_entity_prune``, AC3) was removed
once the backlog it existed to clear was gone (athenaeum#2466) — this write-side
creation gate is the part that stays live.

Covers:
  - the predicate (AC1 file-shaped names excluded; AC2 genuine names survive);
  - the shared creation gate both transports use;
  - configurability (AC4: allowlist wins, toggle off, extension extend);
  - independence from athenaeum#662's stopword mechanism (AC5).
"""

from __future__ import annotations

from athenaeum.models import ClassifiedEntity
from athenaeum.tiers import (
    is_code_artifact_name,
    partition_code_artifact_classifications,
    resolve_junk_match_names,
)

# ---------------------------------------------------------------------------
# is_code_artifact_name — AC1 (excluded) and AC2 (genuine survives)
# ---------------------------------------------------------------------------


class TestIsCodeArtifactName:
    def test_filenames_and_paths_are_code_artifacts(self) -> None:
        # AC1: the representative set the issue names.
        for name in (
            "skill.md",
            "project-registry.yaml",
            "src/athenaeum/librarian.py",
            "AGENTS.md",
            "config.json",
            "deploy.sh",
            "index.ts",
        ):
            assert is_code_artifact_name(name), name

    def test_genuine_entities_resembling_filenames_survive(self) -> None:
        # AC2 (load-bearing): a genuine entity whose name merely resembles a
        # filename is NOT excluded. Multi-word names carry whitespace and are
        # never file-shaped; single words without a code extension survive; a
        # dotted name whose suffix is not a code extension survives.
        for name in (
            "Reach",  # a company literally named "Reach"
            "The Registry",  # multi-word — has whitespace
            "San Francisco",
            "Ada Lovelace",
            "registry",  # bare word, no extension
            "web.dev",  # ".dev" is not a source/config extension
            "Node JS",
            "README",  # no extension
        ):
            assert not is_code_artifact_name(name), name

    def test_blank_name_is_not_a_code_artifact(self) -> None:
        assert not is_code_artifact_name("")
        assert not is_code_artifact_name("   ")


# ---------------------------------------------------------------------------
# Configurability — AC4 (default on, allowlist wins, extensible, toggle)
# ---------------------------------------------------------------------------


class TestConfigurability:
    def test_gate_is_on_by_default(self) -> None:
        assert is_code_artifact_name("skill.md", None) is True
        assert is_code_artifact_name("skill.md", {}) is True

    def test_allowlist_wins(self) -> None:
        # A deployment that legitimately tracks a document by filename.
        cfg = {"librarian": {"code_artifact_allowlist": ["skill.md"]}}
        assert is_code_artifact_name("skill.md", cfg) is False
        # The allowlist is case-insensitive and specific — a sibling filename
        # is still excluded.
        assert is_code_artifact_name("SKILL.MD", cfg) is False
        assert is_code_artifact_name("other.md", cfg) is True

    def test_extension_set_is_extensible(self) -> None:
        assert is_code_artifact_name("notes.rmd", None) is False
        cfg = {"librarian": {"code_artifact_extensions": [".rmd"]}}
        assert is_code_artifact_name("notes.rmd", cfg) is True

    def test_toggle_disables_the_whole_gate(self) -> None:
        cfg = {"librarian": {"exclude_code_artifacts": False}}
        assert is_code_artifact_name("skill.md", cfg) is False
        assert is_code_artifact_name("src/x/y.py", cfg) is False


# ---------------------------------------------------------------------------
# partition_code_artifact_classifications — the shared creation gate
# ---------------------------------------------------------------------------


def _classified(name: str) -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name, entity_type="concept", tags=[], access="public", is_new=True
    )


class TestCreationGate:
    def test_drops_code_artifacts_keeps_genuine(self) -> None:
        classified = [
            _classified("skill.md"),
            _classified("Reach"),
            _classified("src/athenaeum/librarian.py"),
            _classified("Ada Lovelace"),
        ]
        kept, dropped = partition_code_artifact_classifications(classified)
        assert [c.name for c in kept] == ["Reach", "Ada Lovelace"]
        assert dropped == ["skill.md", "src/athenaeum/librarian.py"]

    def test_allowlisted_name_is_kept(self) -> None:
        cfg = {"librarian": {"code_artifact_allowlist": ["skill.md"]}}
        kept, dropped = partition_code_artifact_classifications(
            [_classified("skill.md")], cfg
        )
        assert [c.name for c in kept] == ["skill.md"]
        assert dropped == []


# ---------------------------------------------------------------------------
# AC5 — independence from athenaeum#662's read-side stopword mechanism
# ---------------------------------------------------------------------------


class TestIndependentFrom662:
    def test_gates_are_orthogonal(self) -> None:
        # A athenaeum#662 stopword ("main") is NOT a code artifact (no extension), and a
        # code artifact ("skill.md") is NOT a athenaeum#662 stopword — the two gates are
        # complementary, and athenaeum#680 changes neither the stopword set nor its API.
        junk = resolve_junk_match_names(None)
        assert "main" in junk
        assert not is_code_artifact_name("main")
        assert "skill.md" not in junk
        assert is_code_artifact_name("skill.md")

