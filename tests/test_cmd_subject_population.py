# SPDX-License-Identifier: Apache-2.0
"""``athenaeum subject-population`` — issue athenaeum#1944.

CLI-level coverage for :mod:`athenaeum._cmd_subject_population`, over the
real ``main()`` dispatch. Every LLM-adjacent seam
(``athenaeum.provider.build_llm_client`` / ``athenaeum.search.embed_texts`` /
``athenaeum.tiers._tier2_confirm_same_subject``) is patched with a stub, per
this repo's standing "tests must inject a stub, never rely on real
chromadb/claude-cli" contract (mirrors ``tests/test_subject_population.py``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from athenaeum.cli import main
from athenaeum.models import parse_frontmatter
from athenaeum.subject_population import SubjectRegistry


def _page(
    root: Path,
    filename: str,
    *,
    uid: str,
    name: str,
    type_: str = "concept",
    body: str = "Body text.\n",
) -> Path:
    frontmatter = f"uid: '{uid}'\ntype: {type_}\nname: {name}"
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def _init_git_repo(root: Path) -> None:
    # "git init -b develop, never -b main" -- this repo's own ephemeral
    # test-fixture convention for git-repo fixtures.
    subprocess.run(["git", "init", "-b", "develop"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _commit_all(root: Path, message: str = "init") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r) for r in rows) + ("\n" if rows else "")
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def claude_cli_classify(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATHENAEUM_CLASSIFY_LLM_PROVIDER", "claude-cli")


def _stub_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import athenaeum.provider as provider_mod

    monkeypatch.setattr(provider_mod, "build_llm_client", lambda config, **kwargs: object())


def _stub_embed(monkeypatch: pytest.MonkeyPatch, vector: "list[float] | None" = None) -> None:
    import athenaeum.search as search_mod

    if vector is None:
        monkeypatch.setattr(search_mod, "embed_texts", lambda texts: None)
    else:
        monkeypatch.setattr(search_mod, "embed_texts", lambda texts: [list(vector) for _ in texts])


class TestDryRunDefault:
    def test_no_apply_leaves_wiki_byte_identical_and_writes_one_row_per_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_cli_classify: None
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        a = _page(wiki, "a.md", uid="u1", name="Alpha")
        b = _page(wiki, "b.md", uid="u2", name="Beta")
        before_a, before_b = a.read_bytes(), b.read_bytes()

        _stub_client(monkeypatch)
        _stub_embed(monkeypatch, None)

        report_path = tmp_path / "report.jsonl"
        rc = main(
            ["subject-population", "--path", str(knowledge), "--report", str(report_path)]
        )
        assert rc == 0

        assert a.read_bytes() == before_a
        assert b.read_bytes() == before_b
        assert not (wiki / "_subject_registry.json").exists()
        assert not (wiki / "_pending_questions.md").exists()

        rows = _read_rows(report_path)
        assert {r["uid"] for r in rows} == {"u1", "u2"}


class TestResumeAtCliLevel:
    def test_limit_then_resume_matches_a_single_uninterrupted_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_cli_classify: None
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        _page(wiki, "a.md", uid="u1", name="Alpha")
        _page(wiki, "b.md", uid="u2", name="Beta")
        _page(wiki, "c.md", uid="u3", name="Gamma")

        _stub_client(monkeypatch)
        _stub_embed(monkeypatch, None)

        full_path = tmp_path / "full.jsonl"
        rc = main(["subject-population", "--path", str(knowledge), "--report", str(full_path)])
        assert rc == 0
        full_rows = _read_rows(full_path)
        assert len(full_rows) == 3

        resumed_path = tmp_path / "resumed.jsonl"
        rc1 = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--report",
                str(resumed_path),
                "--limit",
                "1",
            ]
        )
        assert rc1 == 0
        assert len(_read_rows(resumed_path)) == 1

        rc2 = main(
            ["subject-population", "--path", str(knowledge), "--resume", str(resumed_path)]
        )
        assert rc2 == 0
        assert _read_rows(resumed_path) == full_rows


class TestProviderGate:
    def test_api_provider_refuses_before_any_embedding_or_llm_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        _page(wiki, "a.md", uid="u1", name="Alpha")

        monkeypatch.setenv("ATHENAEUM_CLASSIFY_LLM_PROVIDER", "api")

        import athenaeum.provider as provider_mod
        import athenaeum.search as search_mod

        def _boom_client(*args: object, **kwargs: object) -> None:
            raise AssertionError("must never construct an LLM client")

        def _boom_embed(*args: object, **kwargs: object) -> None:
            raise AssertionError("must never call the embedder")

        monkeypatch.setattr(provider_mod, "build_llm_client", _boom_client)
        monkeypatch.setattr(search_mod, "embed_texts", _boom_embed)

        report_path = tmp_path / "report.jsonl"
        rc = main(
            ["subject-population", "--path", str(knowledge), "--report", str(report_path)]
        )
        assert rc != 0
        assert not report_path.exists()


class TestSpendCeilingCliLevel:
    def test_ceiling_trip_stops_before_next_confirmer_call_and_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_cli_classify: None
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        _page(wiki, "a.md", uid="u1", name="Alpha")
        _page(wiki, "b.md", uid="u2", name="Alpha Prime")
        _page(wiki, "c.md", uid="u3", name="Alpha Tertiary")

        _stub_client(monkeypatch)
        _stub_embed(monkeypatch, [1.0, 0.0])

        import athenaeum.tiers as tiers_mod
        from athenaeum.entity_resolution import Match

        def fake_confirm(candidate, top, *, client, config=None, usage=None):
            if usage is not None:
                usage.input_tokens += 1_000_000
            return Match(top[0][0].uid)

        monkeypatch.setattr(tiers_mod, "_tier2_confirm_same_subject", fake_confirm)
        # u1 mints at zero cost (empty pool); u2 is the FIRST confirmer
        # call and crosses the ceiling; the check before u3's resolution
        # (which would ALSO call the confirmer) then trips.
        monkeypatch.setenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN", "500000")

        report_path = tmp_path / "report.jsonl"
        rc = main(
            ["subject-population", "--path", str(knowledge), "--report", str(report_path)]
        )
        assert rc != 0

        rows = _read_rows(report_path)
        assert [r["uid"] for r in rows] == ["u1", "u2"]

        # Resumable: finishing from here reaches u3.
        monkeypatch.delenv("ATHENAEUM_SPEND_MAX_TOKENS_PER_RUN", raising=False)
        rc2 = main(["subject-population", "--path", str(knowledge), "--resume", str(report_path)])
        assert rc2 == 0
        assert {r["uid"] for r in _read_rows(report_path)} == {"u1", "u2", "u3"}


class TestModePairing:
    def test_apply_without_from_report_refuses(self, tmp_path: Path) -> None:
        rc = main(["subject-population", "--path", str(tmp_path), "--apply"])
        assert rc == 2

    def test_from_report_without_apply_refuses(self, tmp_path: Path) -> None:
        report_path = tmp_path / "x.jsonl"
        report_path.write_text("", encoding="utf-8")
        rc = main(
            ["subject-population", "--path", str(tmp_path), "--from-report", str(report_path)]
        )
        assert rc == 2


class TestApplyFromReport:
    def _git_knowledge(self, tmp_path: Path) -> tuple[Path, Path]:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        return knowledge, wiki

    def test_apply_writes_subject_only_where_absent_builds_no_client_saves_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, wiki = self._git_knowledge(tmp_path)
        a = _page(wiki, "a.md", uid="u1", name="Alpha")
        b = _page(wiki, "b.md", uid="u2", name="Beta")
        # b already carries a subject -- must never be overwritten.
        b.write_text(
            b.read_text(encoding="utf-8").replace(
                "name: Beta", "name: Beta\nsubject: subject-999999"
            ),
            encoding="utf-8",
        )
        _init_git_repo(knowledge)
        _commit_all(knowledge)

        report_path = tmp_path / "report.jsonl"
        _write_rows(
            report_path,
            [
                {
                    "uid": "u1",
                    "name": "Alpha",
                    "type": "concept",
                    "path": str(a),
                    "subject": "subject-000001",
                    "reason": "minted",
                    "matched_uid": None,
                    "confirmer_ran": False,
                    "top_k_uids": [],
                },
                {
                    "uid": "u2",
                    "name": "Beta",
                    "type": "concept",
                    "path": str(b),
                    "subject": "subject-000002",
                    "reason": "matched",
                    "matched_uid": "someone-else",
                    "confirmer_ran": True,
                    "top_k_uids": ["someone-else"],
                },
            ],
        )

        import athenaeum.provider as provider_mod

        def _boom_client(*args: object, **kwargs: object) -> None:
            raise AssertionError("apply must never construct an LLM client")

        monkeypatch.setattr(provider_mod, "build_llm_client", _boom_client)

        rc = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--from-report",
                str(report_path),
                "--apply",
            ]
        )
        assert rc == 0

        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        assert meta_a["subject"] == "subject-000001"
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert meta_b["subject"] == "subject-999999"

        registry = SubjectRegistry.load(wiki / "_subject_registry.json")
        assert registry.confirmer_ran.get("subject-000002") is True

    def test_apply_raises_one_pending_question_per_ambiguous_decision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, wiki = self._git_knowledge(tmp_path)
        c = _page(wiki, "c.md", uid="u3", name="Casey Rivera")
        _init_git_repo(knowledge)
        _commit_all(knowledge)

        report_path = tmp_path / "report.jsonl"
        _write_rows(
            report_path,
            [
                {
                    "uid": "u3",
                    "name": "Casey Rivera",
                    "type": "concept",
                    "path": str(c),
                    "subject": "undeterminable",
                    "reason": "undeterminable-ambiguous",
                    "matched_uid": None,
                    "confirmer_ran": True,
                    "top_k_uids": ["u-other-1", "u-other-2"],
                },
            ],
        )

        import athenaeum.provider as provider_mod

        monkeypatch.setattr(
            provider_mod,
            "build_llm_client",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM client")),
        )

        rc = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--from-report",
                str(report_path),
                "--apply",
            ]
        )
        assert rc == 0

        pending = wiki / "_pending_questions.md"
        assert pending.exists()
        assert "Casey Rivera" in pending.read_text(encoding="utf-8")

    def test_refuses_when_not_a_git_repo(self, tmp_path: Path) -> None:
        knowledge, wiki = self._git_knowledge(tmp_path)
        report_path = tmp_path / "report.jsonl"
        _write_rows(report_path, [])
        report_path.write_text('{"uid": "u1"}\n', encoding="utf-8")
        rc = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--from-report",
                str(report_path),
                "--apply",
            ]
        )
        assert rc != 0

    def test_refuses_when_run_lock_is_held(self, tmp_path: Path) -> None:
        knowledge, wiki = self._git_knowledge(tmp_path)
        a = _page(wiki, "a.md", uid="u1", name="Alpha")
        _init_git_repo(knowledge)
        _commit_all(knowledge)

        report_path = tmp_path / "report.jsonl"
        _write_rows(
            report_path,
            [
                {
                    "uid": "u1",
                    "name": "Alpha",
                    "type": "concept",
                    "path": str(a),
                    "subject": "subject-000001",
                    "reason": "minted",
                    "matched_uid": None,
                    "confirmer_ran": False,
                    "top_k_uids": [],
                }
            ],
        )

        from athenaeum.runlock import RunLock

        held = RunLock(knowledge, wait=0)
        held.acquire()
        try:
            rc = main(
                [
                    "subject-population",
                    "--path",
                    str(knowledge),
                    "--from-report",
                    str(report_path),
                    "--apply",
                ]
            )
            assert rc != 0
        finally:
            held.release()

    def test_refuses_when_a_target_page_has_uncommitted_changes(self, tmp_path: Path) -> None:
        knowledge, wiki = self._git_knowledge(tmp_path)
        a = _page(wiki, "a.md", uid="u1", name="Alpha")
        _init_git_repo(knowledge)
        _commit_all(knowledge)
        # Dirty the target page AFTER the commit.
        a.write_text(a.read_text(encoding="utf-8") + "Extra uncommitted content.\n")

        report_path = tmp_path / "report.jsonl"
        _write_rows(
            report_path,
            [
                {
                    "uid": "u1",
                    "name": "Alpha",
                    "type": "concept",
                    "path": str(a),
                    "subject": "subject-000001",
                    "reason": "minted",
                    "matched_uid": None,
                    "confirmer_ran": False,
                    "top_k_uids": [],
                }
            ],
        )

        rc = main(
            [
                "subject-population",
                "--path",
                str(knowledge),
                "--from-report",
                str(report_path),
                "--apply",
            ]
        )
        assert rc != 0
        # Refused before any write: the file's uncommitted content is
        # exactly what we appended, with no subject: line inserted.
        assert "subject:" not in a.read_text(encoding="utf-8")
