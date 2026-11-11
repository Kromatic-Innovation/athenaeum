# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2367 — first recall without an API key.

Covers every acceptance criterion with a MOCKED ``claude`` provider (no real
compile call, no real login — AC6):

- AC1: a fresh root with no ``ANTHROPIC_API_KEY`` and a usable ``claude``
  binary selects ``llm.provider: claude-cli``; a key set keeps the existing
  ``api`` default; an existing config is never rewritten.
- AC3: the SessionEnd-specific 20-file / 100-call caps (already enforced by
  ``athenaeum.librarian.session_end``/``run`` — see ``SESSION_END_MAX_FILES``
  / ``SESSION_END_MAX_API_CALLS``) hold when the active provider is
  ``claude-cli``, and a run stopped at either cap reports what was left.
- AC4: ``athenaeum session-end`` end-to-end under ``claude-cli`` on a fresh
  root — ``remember`` -> ``session_end`` -> ``recall`` returns the fact
  without the ``[uncompiled]`` label. Tier0-structured content (the same
  shape ``tests/test_session_end.py::TestCrossAgentRecall`` already proves
  compiles with zero LLM calls) is used deliberately: this test's job is to
  prove ``session_end``'s own plumbing (provider preflight, lock, compile
  dispatch, reindex) works end-to-end when ``llm.provider: claude-cli`` and
  no ``ANTHROPIC_API_KEY`` is set — not to re-derive the classify/write
  transport contract under the CLI backend, which
  ``tests/test_query_topics.py``'s ``test_claude_cli_provider_makes_zero_sdk_calls``
  and ``tests/test_tiers.py`` already cover independently.
- AC5: the missing-credential counter-example — neither a key nor a usable
  ``claude`` binary — and ``athenaeum init``/``athenaeum doctor`` naming the
  gap and its fix, plus a direct proof that recall still surfaces the saved
  fact (labelled ``[uncompiled]``) rather than staying silently empty.

Every test here either stubs ``shutil.which``/forbids ``anthropic.Anthropic``
construction, or stubs the compile loop directly (``athenaeum.librarian
.process_one``) — none spawns a real ``claude`` subprocess or calls a real
Anthropic endpoint.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum.cli import main as cli_main
from tests.test_budget_deferred import _fake_process_one_factory
from tests.test_session_end import _seed_knowledge_root, _write_tier0_raw

# ---------------------------------------------------------------------------
# AC1 — default provider selection on a fresh root
# ---------------------------------------------------------------------------


class TestDefaultProviderSelectionOnFreshRoot:
    def test_no_key_and_usable_claude_binary_selects_claude_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.config import load_config, write_default_config
        from athenaeum.provider import resolve_provider

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import athenaeum.provider as provider_mod

        monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: "/usr/bin/claude")

        path = write_default_config(tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "llm:\n  provider: claude-cli" in text
        assert resolve_provider(load_config(tmp_path)) == "claude-cli"

    def test_key_set_keeps_existing_api_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.config import load_config, write_default_config
        from athenaeum.provider import resolve_provider

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
        import athenaeum.provider as provider_mod

        # Even with a usable binary present, a configured key keeps the
        # pre-athenaeum#2367 default unchanged.
        monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: "/usr/bin/claude")

        path = write_default_config(tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "llm:\n  provider: claude-cli" not in text
        assert resolve_provider(load_config(tmp_path)) == "api"

    def test_neither_key_nor_binary_keeps_api_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.config import load_config, write_default_config
        from athenaeum.provider import resolve_provider

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import athenaeum.provider as provider_mod

        monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: None)
        monkeypatch.setattr(provider_mod.os.path, "exists", lambda _b: False)

        path = write_default_config(tmp_path)
        text = path.read_text(encoding="utf-8")
        assert "llm:\n  provider: claude-cli" not in text
        assert resolve_provider(load_config(tmp_path)) == "api"

    def test_existing_config_is_never_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The safety property: regardless of key/binary state,
        ``write_default_config`` never touches a config that already
        exists."""
        from athenaeum.config import write_default_config

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import athenaeum.provider as provider_mod

        monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: "/usr/bin/claude")

        config_path = tmp_path / "athenaeum.yaml"
        config_path.write_text("search_backend: fts5\n", encoding="utf-8")

        write_default_config(tmp_path)

        assert config_path.read_text(encoding="utf-8") == "search_backend: fts5\n"


# ---------------------------------------------------------------------------
# AC2 / AC5 — `athenaeum init` disclosure + missing-credential counter-example
# ---------------------------------------------------------------------------


class TestInitDisclosesProvider:
    def test_init_names_claude_cli_and_the_plan_draw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import athenaeum.provider as provider_mod

        monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: "/usr/bin/claude")

        target = tmp_path / "knowledge"
        rc = cli_main(["init", "--path", str(target)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "claude-cli" in out
        assert "plan" in out.lower()

    def test_init_names_api_and_billing_when_key_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")

        target = tmp_path / "knowledge"
        rc = cli_main(["init", "--path", str(target)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "provider: api" in out

    def test_init_names_the_missing_credential_and_fix(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC5 counter-example: neither a key nor a usable `claude` binary —
        `athenaeum init` says what is missing and names the fix, same as
        `athenaeum doctor`'s `check_key_mode`."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import athenaeum.provider as provider_mod

        monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: None)
        monkeypatch.setattr(provider_mod.os.path, "exists", lambda _b: False)

        target = tmp_path / "knowledge"
        rc = cli_main(["init", "--path", str(target)])
        assert rc == 0  # init itself still succeeds — this is advisory
        err = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY" in err
        assert "fix:" in err
        assert "supplying-an-api-key" in err


# ---------------------------------------------------------------------------
# AC3 — SessionEnd's 20-file / 100-call caps hold under claude-cli
# ---------------------------------------------------------------------------


def _pin_claude_cli_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
    import athenaeum.provider as provider_mod

    monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: "/usr/bin/claude")

    import anthropic

    class _ForbiddenSDK:
        def __init__(self, **_: Any) -> None:
            raise AssertionError(
                "anthropic.Anthropic must not be constructed under claude-cli"
            )

    monkeypatch.setattr(anthropic, "Anthropic", _ForbiddenSDK)


class TestSessionEndCapsUnderClaudeCli:
    def test_twenty_file_cap_trips_and_reports_what_was_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.librarian import SESSION_END_MAX_FILES, session_end

        _pin_claude_cli_no_key(monkeypatch)
        root = _seed_knowledge_root(tmp_path)

        n_files = SESSION_END_MAX_FILES + 5
        for i in range(n_files):
            _write_tier0_raw(
                root,
                f"p-{i:04d}",
                f"Person {i}",
                f"20240410T{i:06d}Z",
                f"{i:08x}",
            )
        cache = tmp_path / "cache"

        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            incremental=True,
            cache_dir=cache,
            backend="fts5",
            max_files=SESSION_END_MAX_FILES,
        )

        assert result.exit_code == 0
        assert result.ingest.new_or_changed == n_files
        # The session-end-specific cap (20), not the nightly default (50).
        assert result.ingest.compiled == SESSION_END_MAX_FILES
        remaining = result.ingest.new_or_changed - result.ingest.compiled
        assert remaining == 5, "the run must report exactly what was left uncompiled"

    def test_hundred_call_cap_trips_and_defers_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors ``test_budget_deferred.py``'s own cap-trip proof, pinned
        to the SessionEnd-specific 100-call cap instead of the nightly
        default (800), under claude-cli instead of api."""
        from athenaeum.librarian import SESSION_END_MAX_API_CALLS, run

        _pin_claude_cli_no_key(monkeypatch)
        root = tmp_path / "knowledge"
        root.mkdir()
        (root / "wiki").mkdir()
        sessions = root / "raw" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / ".gitkeep").write_text("")
        subprocess.run(["git", "init", "-q", "-b", "t"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
        )
        subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)

        calls_per_file = 10  # 100 / 10 = cap trips at file 10, well inside
        # SESSION_END_MAX_FILES (20), so this isolates the CALL cap.
        n_files = 15
        for i in range(n_files):
            (sessions / f"2024041{i % 10}T12{i:04d}00Z-aabbcc{i:02d}.md").write_text(
                f"Met with Person {i} about an unrelated topic.\n"
            )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

        monkeypatch.setattr(
            "athenaeum.librarian.process_one", _fake_process_one_factory(calls_per_file)
        )

        rc = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=SESSION_END_MAX_API_CALLS,
            max_files=20,
        )

        assert rc == 0
        manifest = root / "wiki" / "_deferred_work.md"
        assert manifest.exists(), "a call-cap trip must leave a paper trail of what was left"
        text = manifest.read_text(encoding="utf-8")
        assert "deferred_count: 5" in text, text


# ---------------------------------------------------------------------------
# AC4 — session-end end-to-end under claude-cli on a fresh root
# ---------------------------------------------------------------------------


class TestSessionEndUnderClaudeCliEndToEnd:
    def test_remember_then_session_end_then_recall_under_claude_cli(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from athenaeum import transcript_verify
        from athenaeum.librarian import session_end
        from athenaeum.mcp_server import remember_write

        _pin_claude_cli_no_key(monkeypatch)
        root = _seed_knowledge_root(tmp_path)
        cache = tmp_path / "cache"
        monkeypatch.setattr(
            transcript_verify, "default_projects_root", lambda: tmp_path / "projects"
        )

        content = (
            "---\n"
            "uid: p-2367\n"
            "type: person\n"
            "name: Ada Lovelace\n"
            "tags: [active]\n"
            "access: internal\n"
            "originSessionId: sess-2367\n"
            "---\n\n"
            "Ada Lovelace wrote the first published algorithm.\n"
        )
        msg = remember_write(
            root / "raw", content, source="sess-2367", sources="user-stated:e2e"
        )
        assert msg.startswith("Saved to")
        assert not list((root / "wiki").glob("p-2367-*.md"))

        result = session_end(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            session="sess-2367",
            cache_dir=cache,
            backend="fts5",
        )
        assert result.exit_code == 0
        assert result.ingest.compiled == 1
        assert result.reindexed is True
        wiki_pages = list((root / "wiki").glob("p-2367-*.md"))
        assert wiki_pages, "session_end under claude-cli must compile the raw fact into wiki/"

        capsys.readouterr()
        rc = cli_main(
            [
                "recall",
                "Ada Lovelace",
                "--path",
                str(root),
                "--cache-dir",
                str(cache),
                "--backend",
                "fts5",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "lovelace" in out.lower()
        assert "[uncompiled]" not in out, "the fact must surface as COMPILED, not raw"


# ---------------------------------------------------------------------------
# AC5 — missing-credential counter-example does not leave recall silently empty
# ---------------------------------------------------------------------------


class TestMissingCredentialCounterExample:
    def test_recall_still_surfaces_the_fact_as_uncompiled_not_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither a key nor a usable `claude` binary: a compile cannot run,
        but `remember` + `recall` must still surface the raw fact (labelled
        `[uncompiled]`), never a silent empty result."""
        from athenaeum.config import resolve_extra_intake_roots
        from athenaeum.init import init_knowledge_dir
        from athenaeum.mcp_server import _refresh_extra_root_index, recall_search, remember_write

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        import athenaeum.provider as provider_mod

        monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: None)
        monkeypatch.setattr(provider_mod.os.path, "exists", lambda _b: False)

        knowledge = tmp_path / "knowledge"
        init_knowledge_dir(knowledge)
        wiki = knowledge / "wiki"
        raw = knowledge / "raw"
        cache = tmp_path / "cache"

        result = remember_write(
            raw,
            "The vault combination for the backup drive is 7-14-29.",
            sources="user-stated:test",
        )
        assert result.startswith("Saved to")

        _refresh_extra_root_index("fts5", wiki_root=wiki, cache_dir=cache, config=None)
        extra_roots = resolve_extra_intake_roots(knowledge)
        out = recall_search(
            wiki,
            "vault combination backup drive",
            top_k=5,
            search_backend="fts5",
            cache_dir=cache,
            extra_roots=extra_roots,
        )
        assert "7-14-29" in out
        assert "[uncompiled]" in out

    def test_doctor_names_the_missing_credential_with_neither_key_nor_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from athenaeum.doctor import check_key_mode

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
        import athenaeum.provider as provider_mod

        monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: None)
        monkeypatch.setattr(provider_mod.os.path, "exists", lambda _b: False)

        result = check_key_mode(None, tmp_path / "cache")
        assert result.status == "fail"
        assert "ANTHROPIC_API_KEY" in result.detail
        assert "supplying-an-api-key" in result.fix
