"""Code artifacts must not become wiki entities (issue #680).

The librarian was minting durable wiki entities from source-code artifacts
(``skill.md``, ``project-registry.yaml``, ``registry`` -> ``auto-registry.md``).
A wiki page describing a file's PAST state is actively harmful: an agent recalls
it, treats it as current, and spends a session disproving it against the working
tree. This is a WRITE-side class exclusion applied at entity creation — filenames
are an unbounded set a stopword list (#662) cannot enumerate — and a companion
sweep retires the ones already on disk via the existing git-rm retire path.

Covers:
  - the predicate (AC1 file-shaped names excluded; AC2 genuine names survive);
  - the shared creation gate both transports use;
  - configurability (AC4: allowlist wins, toggle off, extension extend);
  - the retire sweep (AC3);
  - independence from #662's stopword mechanism (AC5).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from athenaeum.filename_entity_prune import (
    apply_filename_entity_prune,
    build_filename_entity_report,
)
from athenaeum.models import ClassifiedEntity
from athenaeum.tiers import (
    is_code_artifact_name,
    partition_code_artifact_classifications,
    resolve_junk_match_names,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
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
# AC5 — independence from #662's read-side stopword mechanism
# ---------------------------------------------------------------------------


class TestIndependentFrom662:
    def test_gates_are_orthogonal(self) -> None:
        # A #662 stopword ("main") is NOT a code artifact (no extension), and a
        # code artifact ("skill.md") is NOT a #662 stopword — the two gates are
        # complementary, and #680 changes neither the stopword set nor its API.
        junk = resolve_junk_match_names(None)
        assert "main" in junk
        assert not is_code_artifact_name("main")
        assert "skill.md" not in junk
        assert is_code_artifact_name("skill.md")


# ---------------------------------------------------------------------------
# Retire sweep — AC3 (existing filename-derived pages retired via git rm)
# ---------------------------------------------------------------------------


def _wiki(tmp_path: Path) -> Path:
    kr = tmp_path / "knowledge"
    wiki = kr / "wiki"
    wiki.mkdir(parents=True)

    # Two filename-derived entity pages (the confirmed shapes) + a genuine one.
    (wiki / "919f0485-skill-md.md").write_text(
        "---\nname: skill.md\ntype: concept\n---\nA page minted from a filename.\n",
        encoding="utf-8",
    )
    (wiki / "c56ac256-project-registry-yaml.md").write_text(
        "---\nname: project-registry.yaml\ntype: concept\n---\nAnother filename.\n",
        encoding="utf-8",
    )
    (wiki / "a1-reach.md").write_text(
        "---\nname: Reach\ntype: organization\n---\nA genuine company.\n",
        encoding="utf-8",
    )
    # Index/queue files (``_``-prefixed) must never be touched.
    (wiki / "_pending_questions.md").write_text("- open q\n", encoding="utf-8")

    _git(kr, "init", "-b", "develop")
    _git(kr, "config", "user.email", "t@example.com")
    _git(kr, "config", "user.name", "Prune Test")
    _git(kr, "add", "-A")
    _git(kr, "commit", "-m", "seed")
    return kr


class TestFilenameEntityPrune:
    def test_report_kills_only_filename_derived_pages(self, tmp_path: Path) -> None:
        kr = _wiki(tmp_path)
        report = build_filename_entity_report(kr / "wiki")
        killed = sorted(c.path.name for c in report.kill)
        assert killed == ["919f0485-skill-md.md", "c56ac256-project-registry-yaml.md"]
        # The genuine entity is retained; the _-prefixed queue file is not an
        # entity page and is never scanned.
        retained = [p.name for p, _ in report.retained]
        assert "a1-reach.md" in retained
        assert "_pending_questions.md" not in killed

    def test_apply_git_rms_kill_list_and_is_recoverable(self, tmp_path: Path) -> None:
        kr = _wiki(tmp_path)
        report = build_filename_entity_report(kr / "wiki")
        report = apply_filename_entity_prune(kr, report)

        assert report.committed is True
        assert not (kr / "wiki" / "919f0485-skill-md.md").exists()
        assert not (kr / "wiki" / "c56ac256-project-registry-yaml.md").exists()
        assert (kr / "wiki" / "a1-reach.md").exists()

        show = _git(kr, "show", "--stat", "--format=%s", "HEAD")
        assert "retire 2 filename-derived entity page(s) (#680)" in show.stdout
        # git-recoverable: the removed page survives in history.
        prior = _git(kr, "show", "HEAD~1:wiki/919f0485-skill-md.md")
        assert "skill.md" in prior.stdout

    def test_allowlist_spares_a_page(self, tmp_path: Path) -> None:
        kr = _wiki(tmp_path)
        cfg = {"librarian": {"code_artifact_allowlist": ["skill.md"]}}
        report = build_filename_entity_report(kr / "wiki", config=cfg)
        killed = sorted(c.path.name for c in report.kill)
        assert killed == ["c56ac256-project-registry-yaml.md"]

    def test_refuses_without_git(self, tmp_path: Path) -> None:
        kr = tmp_path / "no-git"
        (kr / "wiki").mkdir(parents=True)
        (kr / "wiki" / "u-skill-md.md").write_text(
            "---\nname: skill.md\n---\nx\n", encoding="utf-8"
        )
        report = build_filename_entity_report(kr / "wiki")
        report = apply_filename_entity_prune(kr, report)
        assert report.committed is False
        assert any("refusing to prune" in e for e in report.errors)
        assert (kr / "wiki" / "u-skill-md.md").exists()

    def test_empty_report_is_noop(self, tmp_path: Path) -> None:
        kr = tmp_path / "knowledge"
        (kr / "wiki").mkdir(parents=True)
        (kr / "wiki" / "a1-reach.md").write_text(
            "---\nname: Reach\n---\nx\n", encoding="utf-8"
        )
        _git(kr, "init", "-b", "develop")
        _git(kr, "config", "user.email", "t@example.com")
        _git(kr, "config", "user.name", "T")
        _git(kr, "add", "-A")
        _git(kr, "commit", "-m", "seed")
        head = _git(kr, "rev-parse", "HEAD").stdout.strip()
        report = apply_filename_entity_prune(
            kr, build_filename_entity_report(kr / "wiki")
        )
        assert report.committed is False
        assert _git(kr, "rev-parse", "HEAD").stdout.strip() == head
