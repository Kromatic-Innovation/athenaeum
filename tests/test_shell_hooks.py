"""Smoke tests for the Claude Code example hooks in ``examples/claude-code/``.

These hooks are load-bearing for the sidecar experience — a regression would
silently break auto-recall for all future sessions. They're shipped to users
via copy-paste, so the CI contract is: each hook must be exit-clean against
a minimal synthetic wiki on a standard POSIX box with ``bash``, ``jq``, and
``sqlite3`` available.

The tests shell out with an isolated ``HOME`` so they never touch the
developer's real ``~/.cache/athenaeum`` or ``~/knowledge``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).parent.parent / "examples" / "claude-code"
SESSION_START = HOOKS_DIR / "session-start-recall.sh"
USER_PROMPT = HOOKS_DIR / "user-prompt-recall.sh"
PRE_COMPACT = HOOKS_DIR / "pre-compact-save.sh"
PENDING_QUESTIONS = HOOKS_DIR / "pending-questions-surface.sh"
WIKI_INJECT = HOOKS_DIR / "wiki-context-inject.sh"
REBUILD_INDEX = HOOKS_DIR / "rebuild-index.sh"


def _require(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not available on this runner")


def _require_hook_python(hook_env: dict[str, str], module: str) -> None:
    """Skip when the hook's python can't import *module* under the isolated HOME.

    The ``hook_env`` fixture isolates ``HOME``, which hides per-user
    site-packages (PEP 370). On machines where athenaeum's dependencies
    are installed in the user site, the hook's python subprocess can't
    import them; the hooks then fail open by design (silent exit 0) and
    these tests would fail on a missing environment precondition rather
    than a hook regression.
    """
    src = Path(hook_env["ATHENAEUM_SRC"]) / "src"
    code = f"import sys; sys.path.insert(0, {str(src)!r}); import {module}"
    proc = subprocess.run(
        [hook_env["ATHENAEUM_PYTHON"], "-c", code],
        env=hook_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        last_line = stderr.splitlines()[-1] if stderr else "unknown error"
        pytest.skip(
            f"hook python cannot import {module} under isolated HOME "
            f"(user-site dependencies hidden): {last_line}"
        )


@pytest.fixture
def hook_env(tmp_path: Path) -> dict[str, str]:
    """Isolated env for hook subprocesses.

    Points HOME at a tmp dir so hooks touch ``$tmp/.cache/athenaeum``
    instead of the developer's real cache, and points KNOWLEDGE_ROOT at a
    synthetic wiki. Inherits PATH so bash/jq/sqlite3 remain discoverable.
    """
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)

    # `memory_tier: hot` (issue athenaeum#1120): user-prompt-recall.sh now
    # filters `WHERE memory_tier = 'hot'`, and an explicit pin resolves
    # ahead of the (warm) class-default `resolve_tier` would otherwise
    # assign an untyped page. Without this, every existing consumer of
    # this shared fixture that asserts a page surfaces through the
    # unprompted-recall hook would regress to a false negative.
    (wiki / "lean-startup.md").write_text(
        "---\n"
        "name: Lean Startup\n"
        "tags: [methodology]\n"
        "description: Build-measure-learn methodology\n"
        "memory_tier: hot\n"
        "---\n\n"
        "The Lean Startup methodology emphasizes rapid iteration and customer feedback.\n"
    )
    (wiki / "customer-development.md").write_text(
        "---\n"
        "name: Customer Development\n"
        "tags: [methodology]\n"
        "description: Steve Blank's four-step framework\n"
        "memory_tier: hot\n"
        "---\n\n"
        "Customer Development is Steve Blank's framework for startup discovery.\n"
    )

    (knowledge / "athenaeum.yaml").write_text(
        "auto_recall: true\nsearch_backend: fts5\n"
    )

    athenaeum_src = Path(__file__).parent.parent

    env = {
        "HOME": str(tmp_path),
        # Belt-and-braces (athenaeum#791): the hooks derive their own cache
        # dir from HOME (see e.g. session-start-recall.sh's
        # ``CACHE_DIR="${HOME}/.cache/athenaeum"``), so redirecting HOME
        # above is already sufficient — but any athenaeum Python code this
        # hook shells out to resolves its cache dir via
        # ``ATHENAEUM_CACHE_DIR env > default``, which falls through to the
        # real ``~/.cache/athenaeum`` if HOME were ever a real home dir
        # (e.g. a future edit that drops the HOME redirect but keeps this
        # dict). Setting it explicitly here closes that route too.
        "ATHENAEUM_CACHE_DIR": str(tmp_path / ".cache" / "athenaeum"),
        "PATH": os.environ.get("PATH", ""),
        "KNOWLEDGE_ROOT": str(knowledge),
        "ATHENAEUM_SRC": str(athenaeum_src),
        "ATHENAEUM_PYTHON": sys.executable,
        # `user-prompt-recall.sh` no longer gates its LLM topic extractor
        # on ANTHROPIC_API_KEY (athenaeum#792), so the extractor branch is
        # reachable under test whenever `command -v $ATHENAEUM_CLI`
        # succeeds. `PATH` above is inherited from the real environment,
        # which may have a genuine `athenaeum` CLI installed — invoking
        # that for real would shell out to `query-topics` and could reach
        # a live LLM (the exact hazard athenaeum#776 and athenaeum#791 are
        # open about).
        # Point ATHENAEUM_CLI at a path that provably does not exist so
        # `command -v` fails deterministically and every test falls
        # through to the regex extractor by construction, not by the
        # accident of ANTHROPIC_API_KEY being unset. Tests that want to
        # exercise the extractor branch itself override this with their
        # own stub.
        "ATHENAEUM_CLI": str(tmp_path / "no-such-athenaeum-binary"),
    }
    return env


class TestSessionStartRecall:
    def test_builds_fts5_index(self, hook_env: dict[str, str], tmp_path: Path) -> None:
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        config_env = tmp_path / ".cache" / "athenaeum" / "config.env"
        assert config_env.is_file()
        body = config_env.read_text()
        assert "AUTO_RECALL=true" in body
        assert "SEARCH_BACKEND=fts5" in body

        index_db = tmp_path / ".cache" / "athenaeum" / "wiki-index.db"
        assert index_db.is_file()

    def test_config_env_and_cache_dir_are_owner_only(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#1179: the cache dir and config.env (which can hold
        ANTHROPIC_API_KEY) must never be group/world-accessible."""
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        cache_dir = tmp_path / ".cache" / "athenaeum"
        config_env = cache_dir / "config.env"
        assert oct(cache_dir.stat().st_mode & 0o777) == oct(0o700)
        assert oct(config_env.stat().st_mode & 0o777) == oct(0o600)

    def test_preexisting_loose_permissions_are_hardened(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#1179: `umask 077` in the hook only governs newly
        *created* files. Both writers in the hook open config.env with
        truncate-write ('w' / shell '>'), which does NOT reset the mode of
        a file that already exists — so a stale config.env left over with
        a loose mode (a manual `touch`, a pre-hardening install, an odd
        platform default) must still be brought back to 0600 on the very
        next run, not left as-is."""
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")

        cache_dir = tmp_path / ".cache" / "athenaeum"
        cache_dir.mkdir(parents=True)
        cache_dir.chmod(0o755)
        config_env = cache_dir / "config.env"
        config_env.write_text("AUTO_RECALL=true\nSEARCH_BACKEND=fts5\n")
        config_env.chmod(0o644)

        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        assert oct(cache_dir.stat().st_mode & 0o777) == oct(0o700)
        assert oct(config_env.stat().st_mode & 0o777) == oct(0o600)

    def test_exits_clean_when_wiki_missing(self, tmp_path: Path) -> None:
        _require("bash")
        env = {
            "HOME": str(tmp_path),
            # See the hook_env fixture's comment (athenaeum#791) for why.
            "ATHENAEUM_CACHE_DIR": str(tmp_path / ".cache" / "athenaeum"),
            "PATH": os.environ.get("PATH", ""),
            "KNOWLEDGE_ROOT": str(tmp_path / "does-not-exist"),
        }
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0


class TestUserPromptRecall:
    def _seed_index(self, hook_env: dict[str, str]) -> None:
        subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

    def test_returns_wiki_match_as_additional_context(
        self, hook_env: dict[str, str]
    ) -> None:
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        stdin_payload = json.dumps(
            {
                "prompt": "Tell me about customer development frameworks",
                "session_id": f"test-{uuid.uuid4().hex}",
            }
        )
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload,
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected hookSpecificOutput JSON on stdout"

        payload = json.loads(result.stdout)
        assert "hookSpecificOutput" in payload, (
            "Claude Code requires additionalContext to be nested under "
            "hookSpecificOutput with hookEventName; flat {'additionalContext': ...} "
            "is silently ignored. See issue athenaeum#39."
        )
        hook_output = payload["hookSpecificOutput"]
        assert hook_output.get("hookEventName") == "UserPromptSubmit"
        assert "Customer Development" in hook_output["additionalContext"]

    def test_attempts_llm_extractor_without_api_key(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#792: the extractor must be *attempted* even with no
        ANTHROPIC_API_KEY set — under `llm.provider: claude-cli` none is
        needed, and the hook must not silently skip it on that basis.

        `$ATHENAEUM_CLI` is pointed at a local stub that records its own
        invocation, never a real `athenaeum` binary — this proves the
        branch is *reached* without the test ever touching a live LLM.

        Uses a placeholder file (not a real FTS5 build via
        `session-start-recall.sh`) to satisfy the hook's early "no index
        at all" bail, so this test does not depend on the `sqlite3` CLI
        being installed on the runner — it only needs to prove the
        extractor call itself is reached.
        """
        _require("bash")
        _require("jq")

        cache_dir = tmp_path / ".cache" / "athenaeum"
        cache_dir.mkdir(parents=True)
        (cache_dir / "wiki-index.db").write_text("")

        marker = tmp_path / "extractor-invoked.marker"
        stub = tmp_path / "athenaeum-stub.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f"echo invoked >> {marker}\n"
            "echo 'customer development'\n"
        )
        stub.chmod(0o755)

        env = dict(hook_env)
        env["ATHENAEUM_CLI"] = str(stub)
        env.pop("ANTHROPIC_API_KEY", None)  # explicit: no key present

        stdin_payload = json.dumps(
            {
                "prompt": "Tell me about customer development frameworks",
                "session_id": f"test-{uuid.uuid4().hex}",
            }
        )
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert marker.is_file(), (
            "extractor stub was never invoked with no ANTHROPIC_API_KEY set — "
            "the gate this test guards against has come back"
        )

    def test_silent_on_short_prompt(self, hook_env: dict[str, str]) -> None:
        _require("bash")
        _require("jq")
        _require("sqlite3")
        self._seed_index(hook_env)

        stdin_payload = json.dumps(
            {
                "prompt": "hi",
                "session_id": f"test-{uuid.uuid4().hex}",
            }
        )
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload,
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exits_clean_with_no_index(self, hook_env: dict[str, str]) -> None:
        _require("bash")
        _require("jq")
        stdin_payload = json.dumps(
            {
                "prompt": "anything at all with enough characters",
                "session_id": f"test-{uuid.uuid4().hex}",
            }
        )
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload,
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""
        # Distinguish "correctly bailed" from "crashed quietly" — a shell
        # error would leave traceback / syntax-error strings on stderr even
        # if exit code is 0 due to a trailing `|| true` or similar. The
        # hook must bail cleanly.
        stderr = result.stderr
        assert "Traceback" not in stderr
        assert "syntax error" not in stderr.lower()
        assert "command not found" not in stderr.lower()

    def test_hot_tier_page_surfaces_non_hot_page_excluded(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issue athenaeum#1120 AC3 — drives a REAL hook invocation end to
        end (not `memory_tiers.select_for_push` in isolation): a `hot`-tier
        page and a `warm`-tier page both match the same query via a real
        index build (`athenaeum.search.FTS5Backend`, schema v4). Only the
        hot page's `memory_tier = 'hot'` predicate in the hook's own SQL
        may let it through.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "hot-widgetronic.md").write_text(
            "---\n"
            "name: Widgetronic Hot Page\n"
            "tags: [widgetronic]\n"
            "description: A hot-tier page about widgetronic devices\n"
            "memory_tier: hot\n"
            "---\n\n"
            "This page discusses widgetronic devices extensively for testing.\n"
        )
        (wiki / "warm-widgetronic.md").write_text(
            "---\n"
            "name: Widgetronic Warm Page\n"
            "tags: [widgetronic]\n"
            "description: A warm-tier page about widgetronic devices\n"
            "memory_tier: warm\n"
            "---\n\n"
            "This page also discusses widgetronic devices extensively for testing.\n"
        )
        self._seed_index(hook_env)

        stdin_payload = json.dumps(
            {
                "prompt": "tell me about widgetronic devices",
                "session_id": f"test-{uuid.uuid4().hex}",
            }
        )
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload,
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected hookSpecificOutput JSON on stdout"

        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Widgetronic Hot Page" in context
        assert "Widgetronic Warm Page" not in context

    def test_push_token_budget_discriminates_tiny_vs_generous(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issue athenaeum#1120 AC4 — a tiny `ATHENAEUM_PUSH_TOKEN_BUDGET`
        must be unable to afford even one entry, while a generous budget on
        the same candidate lets it through. Both assertions are required —
        a test that only checks the generous side can't fail on a budget
        that was never wired up at all.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "hot-budgettest.md").write_text(
            "---\n"
            "name: Budgettest Hot Page\n"
            "tags: [budgettest]\n"
            "description: A hot-tier page about budgettest devices\n"
            "memory_tier: hot\n"
            "---\n\n"
            "This page discusses budgettest devices extensively for testing.\n"
        )
        self._seed_index(hook_env)

        tiny_env = dict(hook_env)
        tiny_env["ATHENAEUM_PUSH_TOKEN_BUDGET"] = "1"
        tiny_payload = json.dumps(
            {
                "prompt": "tell me about budgettest devices",
                "session_id": f"test-{uuid.uuid4().hex}",
            }
        )
        tiny_result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=tiny_payload,
            env=tiny_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert tiny_result.returncode == 0, f"stderr: {tiny_result.stderr}"
        assert tiny_result.stdout == "", (
            "a 1-token budget must not be able to afford any entry — "
            f"got: {tiny_result.stdout!r}"
        )

        generous_env = dict(hook_env)
        generous_env["ATHENAEUM_PUSH_TOKEN_BUDGET"] = "10000"
        generous_payload = json.dumps(
            {
                "prompt": "tell me about budgettest devices",
                "session_id": f"test-{uuid.uuid4().hex}",
            }
        )
        generous_result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=generous_payload,
            env=generous_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert generous_result.returncode == 0, f"stderr: {generous_result.stderr}"
        assert generous_result.stdout, "expected output with a generous budget"
        payload = json.loads(generous_result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Budgettest Hot Page" in context

    def test_legacy_db_without_memory_tier_column_degrades_to_unfiltered(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """Issue athenaeum#1120 — a DB built by an older athenaeum predates
        the `memory_tier` column (schema v4). Selecting a column that
        doesn't exist would raise `sqlite3.OperationalError`, which the
        hook's own `2>/dev/null || echo ""` would otherwise swallow into a
        SILENT ZERO RECALL. The hook must probe for the column and fall
        back to the pre-athenaeum#1120 unfiltered query instead, so an un-rebuilt
        legacy index still surfaces results.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")

        cache_dir = tmp_path / ".cache" / "athenaeum"
        cache_dir.mkdir(parents=True)
        db_path = cache_dir / "wiki-index.db"

        # Build a pre-athenaeum#1120 (schema v3) shaped DB directly: `audience` and
        # `type` present, no `memory_tier` column — what an un-rebuilt
        # index from an older athenaeum install looks like.
        build_script = """
import sqlite3, sys

conn = sqlite3.connect(sys.argv[1])
conn.execute(
    'CREATE VIRTUAL TABLE wiki USING fts5'
    '(filename, name, tags, aliases, description, audience UNINDEXED, '
    'type UNINDEXED, '
    'tokenize="porter unicode61")'
)
conn.execute(
    "INSERT INTO wiki VALUES (?,?,?,?,?,?,?)",
    (
        "legacy-page.md",
        "Legacy Recall Target",
        "legacytierprobe",
        "",
        "A legacy page about legacytierprobe widgets",
        "",
        "person",
    ),
)
conn.commit()
conn.close()
"""
        subprocess.run(
            [hook_env["ATHENAEUM_PYTHON"], "-c", build_script, str(db_path)],
            check=True,
            timeout=10,
        )
        (cache_dir / "config.env").write_text(
            "AUTO_RECALL=true\nSEARCH_BACKEND=fts5\n"
        )

        stdin_payload = json.dumps(
            {
                "prompt": "tell me about legacytierprobe widgets",
                "session_id": f"test-{uuid.uuid4().hex}",
            }
        )
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload,
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, (
            "a legacy (pre-memory_tier) DB must degrade to the unfiltered "
            "query, not silently return zero recall"
        )
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Legacy Recall Target" in context


class TestPreCompactSave:
    def test_emits_system_message_json(self, tmp_path: Path) -> None:
        _require("bash")
        # See the hook_env fixture's comment (athenaeum#791) for why
        # ATHENAEUM_CACHE_DIR is set explicitly alongside HOME.
        env = {
            "HOME": str(tmp_path),
            "ATHENAEUM_CACHE_DIR": str(tmp_path / ".cache" / "athenaeum"),
            "PATH": os.environ.get("PATH", ""),
        }
        result = subprocess.run(
            ["bash", str(PRE_COMPACT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert "systemMessage" in payload
        assert "Knowledge checkpoint" in payload["systemMessage"]


class TestWikiContextInject:
    """`wiki-context-inject.sh` — SessionStart hook that surfaces wiki pages
    matching cwd path keywords, before any prompt is submitted.

    Contract: silent when wiki missing, no keywords match, or cwd is
    generic; emits `[Knowledge context for <project>]` block when at least
    one wiki page matches the cwd-derived keyword set.
    """

    def test_silent_when_wiki_missing(self, tmp_path: Path) -> None:
        _require("bash")
        env = {
            "HOME": str(tmp_path),
            # See the hook_env fixture's comment (athenaeum#791) for why.
            "ATHENAEUM_CACHE_DIR": str(tmp_path / ".cache" / "athenaeum"),
            "PATH": os.environ.get("PATH", ""),
            "KNOWLEDGE_ROOT": str(tmp_path / "does-not-exist"),
        }
        result = subprocess.run(
            ["bash", str(WIKI_INJECT)],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_surfaces_match_when_cwd_keyword_hits_wiki(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        # Add a wiki page whose name/body contains a recognisable token,
        # then run the hook from a directory whose path contains that
        # token. The cwd-keyword grep should pick it up.
        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "innovation-accounting.md").write_text(
            "---\n"
            "name: Innovation Accounting\n"
            "tags: [methodology]\n"
            "---\n\n"
            "Innovation Accounting is a Lean Startup-era measurement framework.\n"
        )
        project_dir = tmp_path / "projects" / "innovation-accounting-toolkit"
        project_dir.mkdir(parents=True)

        result = subprocess.run(
            ["bash", str(WIKI_INJECT)],
            env=hook_env,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[Knowledge context for innovation-accounting-toolkit]" in result.stdout
        assert "Innovation Accounting" in result.stdout

    def test_silent_when_no_keyword_matches(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        # cwd is a unique nonsense string; no wiki page contains it.
        project_dir = tmp_path / "projects" / "qzqzqzqz-no-match-here"
        project_dir.mkdir(parents=True)
        result = subprocess.run(
            ["bash", str(WIKI_INJECT)],
            env=hook_env,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_skips_underscore_index_pages(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "_pending_questions.md").write_text(
            "---\nname: pending\n---\n\nzzunique-token-zz\n"
        )
        project_dir = tmp_path / "projects" / "zzunique-token-zz"
        project_dir.mkdir(parents=True)
        result = subprocess.run(
            ["bash", str(WIKI_INJECT)],
            env=hook_env,
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        # Should not surface the underscore-prefixed page.
        assert result.stdout == ""


class TestRebuildIndex:
    """`rebuild-index.sh` — out-of-band SessionEnd rebuild with atomic lock."""

    def test_builds_fts5_index_into_cache(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")
        result = subprocess.run(
            ["bash", str(REBUILD_INDEX)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        index_db = tmp_path / ".cache" / "athenaeum" / "wiki-index.db"
        assert index_db.is_file()
        log_file = tmp_path / ".cache" / "athenaeum" / "rebuild.log"
        assert log_file.is_file()
        log = log_file.read_text()
        assert "rebuild: start" in log
        assert "rebuild: done" in log

    def test_skips_when_lock_held(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        cache_dir = tmp_path / ".cache" / "athenaeum"
        cache_dir.mkdir(parents=True)
        # Pre-create the lock dir to simulate concurrent rebuild.
        (cache_dir / "rebuild.lock").mkdir()

        result = subprocess.run(
            ["bash", str(REBUILD_INDEX)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Should exit cleanly without crashing into the locked region.
        assert result.returncode == 0
        # Lock dir should still exist (we did not own it, so not removed).
        assert (cache_dir / "rebuild.lock").is_dir()
        log = (cache_dir / "rebuild.log").read_text()
        assert "another rebuild in progress" in log

    def test_exits_clean_when_wiki_missing(self, tmp_path: Path) -> None:
        _require("bash")
        env = {
            "HOME": str(tmp_path),
            # See the hook_env fixture's comment (athenaeum#791) for why.
            "ATHENAEUM_CACHE_DIR": str(tmp_path / ".cache" / "athenaeum"),
            "PATH": os.environ.get("PATH", ""),
            "KNOWLEDGE_ROOT": str(tmp_path / "does-not-exist"),
        }
        result = subprocess.run(
            ["bash", str(REBUILD_INDEX)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0


class TestPendingQuestionsSurface:
    """`pending-questions-surface.sh` — SessionStart hook that surfaces
    unresolved `_pending_questions.md` blocks with a snooze cache.

    Contract: never blocks startup. Empty / missing pending file → silent.
    Populated → prints `[Pending memory questions] N unresolved (oldest: ...)`.
    Snooze file with future date → silent. Past date → re-surfaces.
    """

    def _seed_pending(self, knowledge: Path, count: int = 2) -> None:
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        body = ["# Pending Questions", ""]
        for i in range(count):
            body.append(
                f'## [2026-04-{10 + i:02d}] Entity: "Acme {i}" '
                f"(from sessions/x-{i}.md)"
            )
            body.append(f"- [ ] Question {i}?")
            body.append("**Conflict type**: principled")
            body.append("**Description**: synthetic")
            body.append("")
            body.append("---")
            body.append("")
        (wiki / "_pending_questions.md").write_text("\n".join(body))

    def test_silent_when_no_pending_file(self, hook_env: dict[str, str]) -> None:
        _require("bash")
        # hook_env's wiki has wiki pages but no _pending_questions.md.
        result = subprocess.run(
            ["bash", str(PENDING_QUESTIONS)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_surfaces_count_when_populated(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.cli")
        knowledge = Path(hook_env["KNOWLEDGE_ROOT"])
        self._seed_pending(knowledge, count=3)

        result = subprocess.run(
            ["bash", str(PENDING_QUESTIONS)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "[Pending memory questions]" in result.stdout
        assert "3 unresolved" in result.stdout
        assert "2026-04-10" in result.stdout  # oldest

    def test_silent_when_snoozed_until_future(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        knowledge = Path(hook_env["KNOWLEDGE_ROOT"])
        self._seed_pending(knowledge, count=2)

        cache_dir = tmp_path / ".cache" / "athenaeum"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Far-future ISO instant — must compare > now lexicographically.
        (cache_dir / "pending-questions-snoozed-until").write_text(
            "2999-01-01T00:00:00Z"
        )

        result = subprocess.run(
            ["bash", str(PENDING_QUESTIONS)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_resurfaces_after_snooze_expires(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.cli")
        knowledge = Path(hook_env["KNOWLEDGE_ROOT"])
        self._seed_pending(knowledge, count=1)

        cache_dir = tmp_path / ".cache" / "athenaeum"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Past instant — should be ignored, count surfaces.
        (cache_dir / "pending-questions-snoozed-until").write_text(
            "2000-01-01T00:00:00Z"
        )

        result = subprocess.run(
            ["bash", str(PENDING_QUESTIONS)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "[Pending memory questions]" in result.stdout
        assert "1 unresolved" in result.stdout


class TestKillSwitchHooks:
    """The hooks honour the kill-switch state file / env (issue athenaeum#379).

    The Python side of the same contract is in ``test_kill_switch.py``; these
    assert the bash guards agree — ``all`` scope no-ops every hook, ``compile``
    scope leaves the recall hooks running, and ``ATHENAEUM_DISABLED`` overrides
    the file.
    """

    def _write_disabled(self, home: Path, body: str) -> None:
        cache = home / ".cache" / "athenaeum"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "disabled").write_text(body)

    def test_session_start_noops_when_disabled_all(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        self._write_disabled(tmp_path, '{"scope": "all"}')
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        # No index build happened — the config.env / index db are never written.
        assert not (tmp_path / ".cache" / "athenaeum" / "config.env").exists()
        assert not (tmp_path / ".cache" / "athenaeum" / "wiki-index.db").exists()

    def test_session_start_runs_under_compile_scope(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")
        self._write_disabled(tmp_path, '{"scope": "compile"}')
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        # compile scope leaves recall on — the index IS built.
        assert (tmp_path / ".cache" / "athenaeum" / "wiki-index.db").is_file()

    def test_env_override_noops_session_start(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        env = dict(hook_env)
        env["ATHENAEUM_DISABLED"] = "1"
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert not (tmp_path / ".cache" / "athenaeum" / "config.env").exists()

    def test_empty_disabled_file_noops(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        # An emergency `touch $cache/disabled` (empty file) counts as all-off.
        self._write_disabled(tmp_path, "")
        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert not (tmp_path / ".cache" / "athenaeum" / "config.env").exists()

    def test_user_prompt_recall_noops_when_disabled(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        self._write_disabled(tmp_path, '{"scope": "all"}')
        stdin_payload = json.dumps(
            {"prompt": "customer development", "session_id": "kill-switch-test"}
        )
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            env=hook_env,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_pending_questions_silent_when_disabled(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        self._write_disabled(tmp_path, '{"scope": "all"}')
        result = subprocess.run(
            ["bash", str(PENDING_QUESTIONS)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_rebuild_index_noops_when_disabled(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        _require("bash")
        self._write_disabled(tmp_path, '{"scope": "all"}')
        result = subprocess.run(
            ["bash", str(REBUILD_INDEX)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert not (tmp_path / ".cache" / "athenaeum" / "wiki-index.db").exists()
