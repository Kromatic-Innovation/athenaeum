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

from athenaeum.push_metrics import (
    _parse_ts,
    _query_hash,
    build_push_record,
    durable_push_records_path,
    read_push_records,
    record_push,
)

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

    def test_vector_backend_hot_tier_filter_drops_non_hot_hit(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """Issue athenaeum#1120 (orchestrator review finding) -- the FTS5
        branch enforces `memory_tier = 'hot'` INSIDE its own SQL, but the
        vector branch merged `VECTOR_RESULTS` straight in with no
        equivalent check: under `SEARCH_BACKEND=vector` a warm/cold page
        could bypass the filter entirely -- a dial that looks enforced but
        silently isn't on that deployed path. This drives the REAL hook
        end-to-end with a vector hit and proves the shell-side post-filter
        (a bounded lookup into the SAME `wiki-index.db` verdict, keyed on
        the <=3 filenames the vector backend actually returned) drops a
        non-hot hit and lets a hot one through.

        chromadb's real embedder can't run in this container (the ONNX
        weights host is blocked), so `query_vector_index` is stubbed via
        `ATHENAEUM_SRC` pointing at a fake `src/athenaeum/search.py` that
        returns a fixed row -- the hook's own inline python snippet
        already supports loading an arbitrary `search.py` by path (its
        `importlib.util.spec_from_file_location` branch), so no real
        embedding is needed to prove the SHELL-SIDE filter works.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "hot-vectester.md").write_text(
            "---\n"
            "name: Vectester Hot Page\n"
            "tags: [vectester]\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Unrelated body text, not matched by the probe query below.\n"
        )
        (wiki / "warm-vectester.md").write_text(
            "---\n"
            "name: Vectester Warm Page\n"
            "tags: [vectester]\n"
            "memory_tier: warm\n"
            "---\n\n"
            "Unrelated body text, not matched by the probe query below.\n"
        )
        self._seed_index(hook_env)

        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        (cache_dir / "wiki-vectors").mkdir(parents=True, exist_ok=True)
        config_env = cache_dir / "config.env"
        config_env.write_text(
            config_env.read_text().replace(
                "SEARCH_BACKEND=fts5", "SEARCH_BACKEND=vector"
            )
        )

        # Fake search.py module: query_vector_index ignores the query text
        # entirely and returns a FIXED row -- deterministic, no embedder.
        fake_pkg = tmp_path / "fake-vector-src" / "src" / "athenaeum"
        fake_pkg.mkdir(parents=True)
        vector_env = dict(hook_env)
        vector_env["ATHENAEUM_SRC"] = str(fake_pkg.parent.parent)

        def _set_stub_hit(filename: str, name: str) -> None:
            (fake_pkg / "search.py").write_text(
                "def query_vector_index(query, cache_dir, n=3, exclude=None):\n"
                "    exclude = exclude or set()\n"
                f"    hits = [({filename!r}, {name!r}, 0.9)]\n"
                "    return [h for h in hits if h[0] not in exclude][:n]\n"
            )

        # A prompt whose terms appear in neither page's body/frontmatter --
        # isolates the assertion to the vector path; FTS5 contributes
        # nothing, so a leak can only come from the vector branch.
        probe_prompt = "zzznonmatchingzzz term completely unrelated content"

        _set_stub_hit("warm-vectester.md", "Vectester Warm Page")
        warm_result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=json.dumps(
                {"prompt": probe_prompt, "session_id": f"test-{uuid.uuid4().hex}"}
            ),
            env=vector_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert warm_result.returncode == 0, f"stderr: {warm_result.stderr}"
        assert warm_result.stdout == "", (
            "a warm-tier vector hit must be dropped by the shell-side "
            f"post-filter, not surfaced -- got: {warm_result.stdout!r}"
        )

        # Positive control: same stub, hot page instead -- proves the
        # filter isn't a no-op that happens to eat every vector hit.
        _set_stub_hit("hot-vectester.md", "Vectester Hot Page")
        hot_result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=json.dumps(
                {"prompt": probe_prompt, "session_id": f"test-{uuid.uuid4().hex}"}
            ),
            env=vector_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert hot_result.returncode == 0, f"stderr: {hot_result.stderr}"
        assert hot_result.stdout, "expected a hot-tier vector hit to surface"
        payload = json.loads(hot_result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Vectester Hot Page" in context

    # -- issue athenaeum#1343: sidecar push telemetry -----------------------

    def _run_hook(
        self, env: dict[str, str], prompt: str, session_id: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        payload = json.dumps(
            {"prompt": prompt, "session_id": session_id or f"test-{uuid.uuid4().hex}"}
        )
        return subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_description_less_page_records_a_nonzero_token_cost(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issue athenaeum#1344 AC: "the ledger's cost accounting must not
        silently keep reporting the old name-only figure."

        Regression test for a field-shift that reported something worse
        than a stale figure -- it reported ZERO. `description` is absent
        on ~14% of the corpus, and bash's `read` treats TAB as IFS
        *whitespace* whatever IFS is set to, so an empty `description`
        field collapsed and shifted every later field left: `cost` fell
        off the end, the numeric guard defaulted it to 0, and every
        description-less page recorded `token_cost: 0`. The ledger's own
        cost accounting reading as zero is exactly the "reads as zero
        forever" hazard athenaeum#1343 exists to close.

        Both pages must be present in one push: the described page proves
        the row is otherwise sane, and the description-less page is the
        one that used to record zero.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "described-widgetronic.md").write_text(
            "---\n"
            "name: Widgetronic Described\n"
            "tags: [widgetronic]\n"
            "description: A page about widgetronic devices and their calibration.\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Widgetronic devices discussed here.\n"
        )
        # No `description:` key at all -- the ~14% case.
        (wiki / "bare-widgetronic.md").write_text(
            "---\n"
            "name: Widgetronic Bare\n"
            "tags: [widgetronic]\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Widgetronic devices discussed here too.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about widgetronic devices")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected hookSpecificOutput JSON on stdout"

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        rec = records[0]

        by_id = {it["id"]: it for it in rec["items"]}
        assert "bare-widgetronic.md" in by_id, (
            "the description-less page must be pushed and recorded; got "
            f"{sorted(by_id)}"
        )

        for page_id, item in by_id.items():
            assert item["token_cost"] > 0, (
                f"{page_id} recorded token_cost={item['token_cost']}; every "
                "pushed bullet costs at least one token, and a 0 here means "
                "the tab-delimited row shifted and `cost` fell off the end"
            )

        assert rec["token_cost"] == sum(it["token_cost"] for it in rec["items"]), (
            "the record's aggregate token_cost must equal the sum of its "
            "items -- a shifted field understates the aggregate too"
        )

    def test_push_telemetry_round_trips_through_read_push_records(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC: the record's top-level shape is byte-compatible with
        `PushRecord.to_dict()` so `read_push_records()` parses a
        hook-written row unmodified. Also covers the Plan step 6
        assertion: with the hot-tier gate still in place, every recorded
        item's `memory_tier` is `"hot"` -- the gate excludes anything
        else from ever being pushed at all.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        result = self._run_hook(
            hook_env, "tell me about customer development frameworks"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected hookSpecificOutput JSON on stdout"

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        rec = records[0]

        assert rec["v"] == 1
        assert isinstance(rec["session_id"], str) and rec["session_id"]
        assert isinstance(rec["ts"], str)
        assert isinstance(rec["query_hash"], str) and len(rec["query_hash"]) == 16
        assert rec["backend"] == "fts5"
        assert isinstance(rec["items"], list) and len(rec["items"]) == 1
        assert rec["pushed_count"] == len(rec["items"])
        assert isinstance(rec["token_cost"], int)
        assert rec["token_cost_estimated"] is True
        assert rec["source"] == "sidecar"

        item = rec["items"][0]
        assert isinstance(item["id"], str) and item["id"]
        assert item["tier"] == "internal"
        assert isinstance(item["scope"], str)
        assert isinstance(item["token_cost"], int)
        assert isinstance(item["relevance"], float)
        assert item["backend"] == "fts5"
        # Plan step 6: the gate stays in this issue, so every item pushed
        # by the hook is, by construction, memory_tier == "hot".
        assert item["memory_tier"] == "hot"

    def test_id_derivation_uid_prefix_and_timestamp_fallback(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC 'id is never a name-derived slug', both required
        counter-examples: `49eb5d0e-enrico-bruschini.md` records exactly
        `49eb5d0e` (and no substring of the person's name appears
        anywhere in the row); `20260802T023311Z-3f0ea402.md` records the
        full filename, matching `opaque_push_id`'s Python fallback.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        # The FTS5 `wiki` table indexes filename/name/tags/aliases/
        # description only (no body text — see the schema in the module
        # header) — the shared probe term must live in `description`,
        # matching every other fixture in this file.
        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "49eb5d0e-enrico-bruschini.md").write_text(
            "---\n"
            "name: Enrico Bruschini\n"
            "tags: [person]\n"
            "description: A page about widgetronicuidtest for id-derivation testing\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Enrico Bruschini body text, not indexed.\n"
        )
        (wiki / "20260802T023311Z-3f0ea402.md").write_text(
            "---\n"
            "name: Raw Intake Page\n"
            "tags: [raw]\n"
            "description: A raw intake page about widgetronicuidtest for id-derivation testing\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Raw intake body text, not indexed.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about widgetronicuidtest")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        wiki_root = wiki
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        ids = {item["id"] for item in records[0]["items"]}
        assert "49eb5d0e" in ids
        assert "20260802T023311Z-3f0ea402.md" in ids

        raw_line = durable_push_records_path(wiki_root, cache_dir=cache_dir).read_text()
        assert "enrico" not in raw_line.lower()
        assert "bruschini" not in raw_line.lower()

    def test_source_discriminator_partitions_mixed_ledger(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC counter-example: a ledger containing both an MCP `recall`
        record (no `source` key, written via the real `record_push`) and
        a sidecar record must partition into exactly 1 + 1 on
        `rec.get("source") == "sidecar"` (D1).
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        self._seed_index(hook_env)

        wiki_root = wiki
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])

        # An authentic MCP-path row, written by the real production
        # function -- not a hand-rolled dict -- to the SAME resolved
        # ledger location the hook will append to.
        mcp_record = build_push_record(
            session_id="mcp-session-1",
            query="an explicit recall query",
            backend="fts5",
            hits=[("some-mcp-page.md", {}, "a rendered snippet of text")],
        )
        assert record_push(mcp_record, cache_dir=cache_dir, wiki_root=wiki_root)

        result = self._run_hook(
            hook_env,
            "tell me about customer development frameworks",
            f"sidecar-sess-{uuid.uuid4().hex}",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 2
        sidecar = [r for r in records if r.get("source") == "sidecar"]
        recall = [r for r in records if r.get("source") != "sidecar"]
        assert len(sidecar) == 1
        assert len(recall) == 1
        assert recall[0]["session_id"] == "mcp-session-1"

    def test_query_hash_matches_push_metrics_query_hash(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC: `query_hash` computed identically to
        `push_metrics._query_hash` for the SAME probe string -- asserted
        against the real function, not a hardcoded hex string. The raw
        prompt text is never written to the ledger.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        probe = "tell me about customer development frameworks"
        result = self._run_hook(hook_env, probe)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        assert records[0]["query_hash"] == _query_hash(probe)

        raw_line = durable_push_records_path(wiki_root, cache_dir=cache_dir).read_text()
        assert probe not in raw_line

    def test_ledger_path_legacy_branch_when_only_legacy_populated(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC (post-edit): a populated legacy `<cache_dir>/_push_records.jsonl`
        with no `<wiki_root>` file -- both the hook and
        `durable_push_records_path` must resolve LEGACY."""
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        new_path = wiki_root / "_push_records.jsonl"
        legacy_path = cache_dir / "_push_records.jsonl"
        assert not new_path.exists()
        legacy_path.write_text('{"pre-existing":"legacy-row"}\n')

        # Python's own resolution must agree BEFORE the hook ever runs.
        assert durable_push_records_path(wiki_root, cache_dir=cache_dir) == legacy_path

        result = self._run_hook(hook_env, "tell me about customer development frameworks")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        assert not new_path.exists(), "must not also write the new-path ledger"
        lines = legacy_path.read_text().strip().splitlines()
        assert len(lines) == 2
        appended = json.loads(lines[1])
        assert appended["source"] == "sidecar"

        # And the resolution rule still agrees after the write.
        assert durable_push_records_path(wiki_root, cache_dir=cache_dir) == legacy_path

    def test_ledger_path_new_branch_when_neither_file_present(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC (post-edit): a tmpdir with NEITHER file present -- both the
        hook and `durable_push_records_path` resolve to the *new*
        `<wiki_root>` path. (Paired with the legacy-branch test above --
        this case alone would pass vacuously and prove nothing.)
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        new_path = wiki_root / "_push_records.jsonl"
        legacy_path = cache_dir / "_push_records.jsonl"
        assert not new_path.exists()
        assert not legacy_path.exists()

        assert durable_push_records_path(wiki_root, cache_dir=cache_dir) == new_path

        result = self._run_hook(hook_env, "tell me about customer development frameworks")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        assert new_path.is_file()
        assert not legacy_path.exists(), "must not also write the legacy ledger"
        assert durable_push_records_path(wiki_root, cache_dir=cache_dir) == new_path

    def test_push_metrics_enabled_gate_honoured(self, hook_env: dict[str, str]) -> None:
        """AC: honours `ATHENAEUM_PUSH_METRICS_ENABLED` with the SAME
        precedence and falsey-token set as
        `config.resolve_push_metrics_enabled`. Three cases, all required:
        an explicit falsey token is off; unset is on (default); and a
        SET-but-EMPTY value is ALSO off (D10's asymmetry) -- a naive
        `${VAR:-default}` shell expansion would get this case wrong by
        conflating "unset" with "set empty". In every case the
        `[Knowledge context]` push itself is unaffected.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        ledger_path = wiki_root / "_push_records.jsonl"

        off_env = dict(hook_env)
        off_env["ATHENAEUM_PUSH_METRICS_ENABLED"] = "false"
        off_result = self._run_hook(
            off_env,
            "tell me about customer development frameworks",
            f"gate-off-{uuid.uuid4().hex}",
        )
        assert off_result.returncode == 0, f"stderr: {off_result.stderr}"
        assert "Customer Development" in off_result.stdout, (
            "the recall push itself must be unaffected by the telemetry gate"
        )
        assert not ledger_path.exists(), "an explicit falsey token must write nothing"

        empty_env = dict(hook_env)
        empty_env["ATHENAEUM_PUSH_METRICS_ENABLED"] = ""
        empty_result = self._run_hook(
            empty_env,
            "tell me about customer development frameworks",
            f"gate-empty-{uuid.uuid4().hex}",
        )
        assert empty_result.returncode == 0, f"stderr: {empty_result.stderr}"
        assert "Customer Development" in empty_result.stdout
        assert not ledger_path.exists(), (
            "D10 asymmetry: a SET-but-EMPTY env value must be treated as "
            "falsey (off), same as an explicit '0'/'false' token"
        )

        on_result = self._run_hook(
            hook_env,
            "tell me about customer development frameworks",
            f"gate-default-on-{uuid.uuid4().hex}",
        )
        assert on_result.returncode == 0, f"stderr: {on_result.stderr}"
        assert "Customer Development" in on_result.stdout
        assert ledger_path.is_file(), "unset env must fall through to the default (on)"

    def test_ledger_write_failure_never_breaks_the_push(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """AC: a ledger-write failure never breaks or delays the push.
        Points the ledger's resolved directory at a path that cannot be
        created (a regular file sits where a parent directory would need
        to exist -- fails even when the test runs as root, unlike a
        chmod-based block) and confirms the `[Knowledge context]` block
        is still emitted unchanged and the hook exits 0.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        # Seed the index against the NORMAL wiki root first -- only the
        # subsequent hook invocation's ledger-path resolution is broken,
        # not the index build itself.
        self._seed_index(hook_env)

        blocker_file = tmp_path / "not-a-directory"
        blocker_file.write_text("this is a file, not a directory")
        broken_env = dict(hook_env)
        broken_env["KNOWLEDGE_WIKI_PATH"] = str(blocker_file / "wiki")

        result = self._run_hook(
            broken_env, "tell me about customer development frameworks"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Customer Development" in context

    def test_turn_that_pushes_nothing_writes_nothing(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC: a turn that pushes nothing writes nothing (mirrors
        `record_push`'s own `if not record.session_id or not
        record.items: return False`)."""
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        result = self._run_hook(
            hook_env, "zzznonmatchingzzz query with no candidates at all whatsoever"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout == "", f"expected no push, got: {result.stdout!r}"
        assert not (wiki_root / "_push_records.jsonl").exists()
        assert not (cache_dir / "_push_records.jsonl").exists()

    def test_fts5_path_starts_no_python_interpreter(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issue athenaeum#1343 AC: "No Python interpreter start is added to
        the FTS5 path."

        The wall-clock half of that criterion is hardware-bound and cannot
        be asserted from a CI container (whose absolute floor already sits
        above the hook's own <50ms header contract, before AND after this
        change). The *structural* half can be, permanently and on every
        machine: point `$ATHENAEUM_PYTHON` at a recording stub and assert
        the FTS5 path never invokes it. That is the invariant the latency
        contract actually rests on — a Python interpreter start measured
        360-450ms warm / ~1090ms cold on the author's box (see this hook's
        header), i.e. two orders of magnitude above the telemetry append's
        own cost.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        # Seed AFTER the index build (which legitimately uses Python) so
        # the stub only observes the per-turn hook.
        tmp = Path(hook_env["HOME"])
        marker = tmp / "python-was-started"
        stub = tmp / "python-stub"
        stub.write_text(f'#!/usr/bin/env bash\necho started >> {marker}\nexit 1\n')
        stub.chmod(0o755)

        env = dict(hook_env)
        env["ATHENAEUM_PYTHON"] = str(stub)

        result = self._run_hook(env, "tell me about customer development frameworks")

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected the push to still be emitted"
        assert not marker.exists(), (
            "the FTS5 path must not start a Python interpreter — the "
            "telemetry append added by issue athenaeum#1343 is shell/awk plus at "
            f"most one sha256 subprocess. Stub invocations: "
            f"{marker.read_text() if marker.exists() else ''!r}"
        )

    def test_concurrent_hook_runs_never_interleave_a_partial_line(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC: appends are durable and atomic against concurrent
        sessions -- two hooks running concurrently never interleave a
        partial line. Fires N concurrent hook invocations (distinct
        session ids, so no run is suppressed by another's session-scoped
        dedup file) and asserts every resulting ledger line parses as
        complete JSON and the line count matches N.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        n = 12
        procs = [
            subprocess.Popen(
                ["bash", str(USER_PROMPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=hook_env,
                text=True,
            )
            for _ in range(n)
        ]
        for i, proc in enumerate(procs):
            payload = json.dumps(
                {
                    "prompt": "tell me about customer development frameworks",
                    "session_id": f"conc-{i}-{uuid.uuid4().hex}",
                }
            )
            proc.stdin.write(payload)
            proc.stdin.close()
        for proc in procs:
            assert proc.wait(timeout=15) == 0

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        ledger_path = durable_push_records_path(wiki_root, cache_dir=cache_dir)
        assert ledger_path.is_file()
        lines = ledger_path.read_text().splitlines()
        assert len(lines) == n
        for line in lines:
            row = json.loads(line)  # raises if a line is torn/interleaved
            assert row["source"] == "sidecar"

    def test_parse_ts_accepts_the_emitted_ts(self, hook_env: dict[str, str]) -> None:
        """AC (D9): `_parse_ts` accepts the hook's emitted `ts` — second
        resolution, Z-suffixed, no microseconds."""
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about customer development frameworks")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        parsed = _parse_ts(records[0]["ts"])
        assert parsed is not None

    # -- issue athenaeum#1343 review findings (defects 1-3) ------------------

    def test_tab_in_indexed_name_does_not_break_the_push(
        self, hook_env: dict[str, str]
    ) -> None:
        """Defect 1 (structural): a page whose indexed `name` column
        contains a literal tab shifts every field after it in the
        7-field `read` the telemetry pass parses -- `read` dumps all
        overflow into the LAST variable (`cost`), which then fails
        `_pm_is_number` and used to reach bash arithmetic un-guarded.
        Under `set -euo pipefail`, arithmetic on a non-numeric token that
        LOOKS like a bare identifier triggers a `set -u` unbound-variable
        abort -- which, unlike an ordinary command failure, is NOT
        suppressed by wrapping the caller in `|| true` (see
        `_pm_record_push`'s header comment for the two verifying probes).
        The fix moved telemetry construction out of the render loop
        entirely (into `_pm_record_push`, invoked once, after the render
        loop already built the `[Knowledge context]` block) so a crash
        inside telemetry construction can never prevent that block from
        being emitted. This is a REAL trigger, not a hypothetical -- a
        tab in an indexed `name` is exactly what athenaeum#1344's
        `description` field is required to survive too.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "tab-page.md").write_text(
            "---\n"
            'name: "Tab\tHere Page"\n'
            "tags: [tabtest]\n"
            "description: A page about tabbrokentest for regression testing\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Body text, not indexed.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about tabbrokentest")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "the push must survive a tab embedded in an indexed column"
        payload = json.loads(result.stdout)
        assert "hookSpecificOutput" in payload
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert payload["hookSpecificOutput"]["additionalContext"]

    def test_degenerate_relevance_field_still_writes_valid_json(
        self, hook_env: dict[str, str]
    ) -> None:
        """Defect 3: `relevance` must never be interpolated unquoted when
        it could be empty or non-numeric -- `"relevance":,` is malformed
        JSON `read_push_records` cannot parse (the exact "reads as zero
        forever" hazard this issue exists to prevent). Reuses the same
        tab-shifted-field trigger as the defect-1 test (a real repro, not
        a synthetic one) but asserts a DIFFERENT thing: that whatever
        ledger line results is still syntactically valid JSON, and that
        the corrupted (non-numeric) `rank` value the shift produces is
        guarded down to a JSON `null` rather than emitted raw.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "tab-page.md").write_text(
            "---\n"
            'name: "Tab\tHere Page"\n'
            "tags: [tabtest]\n"
            "description: A page about tabbrokentest for regression testing\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Body text, not indexed.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about tabbrokentest")
        assert result.returncode == 0, f"stderr: {result.stderr}"

        wiki_root = wiki
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        ledger_path = durable_push_records_path(wiki_root, cache_dir=cache_dir)
        assert ledger_path.is_file()
        lines = ledger_path.read_text().strip().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])  # raises if malformed -- e.g. "relevance":,
        assert row["items"][0]["relevance"] is None

    def test_pm_is_number_rejects_non_numeric_and_accepts_scientific_notation(
        self,
    ) -> None:
        """Defect 3 (unit-level): the numeric guard used before ANY value
        reaches bash arithmetic or unquoted JSON interpolation. Sourced
        directly from the shipped hook (not reimplemented here) so this
        test tracks the real function, not a copy that could drift.
        Sqlite's FTS5 `rank` legitimately produces scientific notation
        (e.g. `-1.0e-06`) -- that must be ACCEPTED, not rejected as
        "non-numeric".
        """
        _require("bash")
        extracted = subprocess.run(
            ["sed", "-n", "/^_pm_is_number() {/,/^}/p", str(USER_PROMPT)],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        assert extracted.strip(), "could not extract _pm_is_number from the hook"

        def _check(value: str) -> bool:
            script = f"{extracted}\n_pm_is_number {value!r} && echo yes || echo no\n"
            proc = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert proc.returncode == 0, f"stderr: {proc.stderr}"
            return proc.stdout.strip() == "yes"

        assert _check("12") is True
        assert _check("-3.64558386950812e-06") is True
        assert _check("-1.0e-06") is True
        assert _check("0") is True
        assert _check("") is False
        assert _check("fts5") is False
        assert _check("fts5\t12") is False
        assert _check("12abc") is False

    def test_scope_from_audience_has_no_bash4_only_array_syntax(self) -> None:
        """Defect 2 (structural guard): `#!/usr/bin/env bash` on stock
        macOS resolves to `/bin/bash`, GNU bash 3.2.57 (Apple never
        shipped a newer bash after the GPLv3 relicense) -- and this repo
        already has precedent (athenaeum#1104) for removing a bash-4-only
        construct (`mapfile`) from `scripts/public-safe-lint-gate.sh` for
        exactly this reason. Under bash 3.2 with `set -u`, referencing an
        empty array can raise "unbound variable"; `_pm_scope_from_audience`
        used to reach exactly that state on a public-marker-only audience
        (a normal public page). This asserts no bash array syntax
        (`local -a` / `declare -a` / `+=(` array-append) survives
        anywhere in the hook, not just in that one function -- a
        regression here is a silent bash-3.2 landmine, not a test
        failure on THIS box (which runs bash 5.2).
        """
        text = USER_PROMPT.read_text()
        assert "local -a" not in text
        assert "declare -a" not in text
        assert "+=(" not in text

    def test_scope_from_audience_public_marker_only_resolves_open(
        self, hook_env: dict[str, str]
    ) -> None:
        """Defect 2 (functional): the exact audience shape that used to
        crash under bash 3.2 -- `|__access_open__|`, a public page with
        NO roles -- must resolve to `scope: "open"` end to end, proving
        the array-free rewrite is still correct, not just array-free.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "public-only-page.md").write_text(
            "---\n"
            "name: Public Only Page\n"
            "tags: [pubtest]\n"
            "description: A page about pubonlytest for scope regression testing\n"
            "memory_tier: hot\n"
            "access: open\n"
            "---\n\n"
            "Body text, not indexed.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about pubonlytest")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        wiki_root = wiki
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        assert records[0]["items"][0]["scope"] == "open"

    # -- issue athenaeum#1344: render the page summary, rank by relevance --

    def test_empty_description_renders_without_dangling_separator(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC counter-example: a page with an ABSENT `description` (14%
        of the corpus, per the issue's own measurement) must render
        exactly as it did before athenaeum#1344 -- the bare name, no
        dangling ` — ` separator.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "no-description-page.md").write_text(
            "---\n"
            "name: Nodescriptiontest Page\n"
            "tags: [nodescriptiontest]\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Body text about nodescriptiontest, not indexed.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about nodescriptiontest")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "  - Nodescriptiontest Page\n" in context
        assert "—" not in context

    def test_long_description_clamped_not_pushed_in_full(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC counter-example: a page whose `description` is 5,000
        characters must not push a 5,000-character bullet -- it renders
        clamped to the 200-char authoring-convention bound. Uses an
        accented multi-byte character (the issue's own "beware
        truncating mid-UTF-8-character" warning, and the corpus's own
        accented-name precedent) so a byte-oriented clamp that split a
        multi-byte sequence would either corrupt the JSON payload (this
        test's own `json.loads` would raise) or land short of/past 200
        visible characters -- this test catches either.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        long_desc = "é" * 5000
        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "longdesctest-page.md").write_text(
            "---\n"
            "name: Longdesctest Page\n"
            "tags: [longdesctest]\n"
            f"description: {long_desc}\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Body text about longdesctest, not indexed.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about longdesctest")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        payload = json.loads(result.stdout)  # raises if the clamp split a byte
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert long_desc not in context, (
            "a 5,000-character description must not be pushed in full"
        )
        marker = "Longdesctest Page — "
        assert marker in context
        start = context.index(marker) + len(marker)
        rendered_desc = context[start : context.index("\n", start)]
        assert rendered_desc == "é" * 200, (
            f"expected exactly 200 clamped characters, got {len(rendered_desc)}"
        )

    def test_tab_in_description_does_not_shift_fields(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC counter-example: a literal tab embedded in `description`
        must not shift the awk field positions in the budget pass -- the
        SAME class of hazard issue athenaeum#1343 already found and fixed
        for `name` (see the tab-in-name regression tests above), now
        closed for `description` at the SQL source (the `${DESC_COL}`
        expression collapses tab/newline/CR to a space before the value
        ever reaches the tab-separated pipeline) rather than merely
        tolerated downstream. The tab is embedded on a single physical
        line (mirroring the tab-in-`name` fixture's own approach) so it
        survives the real frontmatter-authoring path intact -- a raw
        newline, by contrast, cannot (verified separately: the per-line
        frontmatter parser either truncates at it or folds a continuation
        line back in with a space), so that hazard is covered by the
        raw-SQL-built fixture below instead.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "tab-desc-page.md").write_text(
            "---\n"
            "name: Tabdesctest Page\n"
            "tags: [tabdesctest]\n"
            'description: "A description with a\ttab for tabdesctest regression"\n'
            "memory_tier: hot\n"
            "---\n\n"
            "Body text about tabdesctest, not indexed.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about tabdesctest")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "a tab embedded in `description` must not break the push"

        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "\t" not in context
        assert "A description with a tab for tabdesctest regression" in context

        wiki_root = wiki
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        # The ledger's own token_cost must still be a valid, non-corrupted
        # number -- proving the tab didn't shift `cost` into `backend`.
        item = records[0]["items"][0]
        assert isinstance(item["token_cost"], int)
        assert item["token_cost"] > 0
        assert item["backend"] == "fts5"

    def test_description_with_quote_backslash_newline_still_yields_valid_json(
        self, hook_env: dict[str, str]
    ) -> None:
        """Required test (issue athenaeum#1344 brief, section 2 "the raw-into-
        JSON hazard"): a description containing a double quote, a
        backslash, AND a raw embedded newline -- the exact combination
        the brief names -- must still round-trip through `json.loads`,
        and the description must survive READABLY (not silently dropped
        to protect JSON validity).

        A raw newline can never reach `description` through the normal
        frontmatter-authoring path (verified separately: the per-line
        frontmatter parser terminates the value at a raw newline rather
        than embedding it), so this builds the FTS5 table by hand,
        matching the legacy-DB tests' approach above, to prove the
        hook's own SQL-level sanitisation and `_pm_json_escape` call
        handle a value however it arrived in the index, not just one
        that could plausibly be authored.

        Proven as a real oracle, not a vacuous pass: reverting the
        `_pm_json_escape "$bullet"` call in the render loop back to raw
        `${bullet}` interpolation makes this assertion fail with
        `json.JSONDecodeError` (verified by hand while building this
        test) -- the escaping is load-bearing, not redundant.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")

        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        db_path = cache_dir / "wiki-index.db"

        hazard_description = (
            'A "quoted" word, a back\\slash, and\na raw newline, for hazardtest'
        )
        build_script = """
import sqlite3, sys

conn = sqlite3.connect(sys.argv[1])
conn.execute(
    'CREATE VIRTUAL TABLE wiki USING fts5'
    '(filename, name, tags, aliases, description, audience UNINDEXED, '
    'type UNINDEXED, memory_tier UNINDEXED, '
    'tokenize="porter unicode61")'
)
conn.execute(
    "INSERT INTO wiki VALUES (?,?,?,?,?,?,?,?)",
    (
        "hazard-page.md",
        "Hazardtest Page",
        "hazardtest",
        "",
        sys.argv[2],
        "|",
        "person",
        "hot",
    ),
)
conn.commit()
conn.close()
"""
        subprocess.run(
            [
                hook_env["ATHENAEUM_PYTHON"],
                "-c",
                build_script,
                str(db_path),
                hazard_description,
            ],
            check=True,
            timeout=10,
        )
        (cache_dir / "config.env").write_text("AUTO_RECALL=true\nSEARCH_BACKEND=fts5\n")

        result = self._run_hook(hook_env, "tell me about hazardtest")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected a push despite the hazardous description"

        # The real oracle: this must be VALID JSON.
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Hazardtest Page" in context
        assert '"quoted"' in context
        assert "back\\slash" in context
        assert "raw newline" in context

    def test_legacy_db_without_description_column_degrades_to_name_only(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC 'Legacy-DB safety is preserved' / required test 5: mirrors
        `test_legacy_db_without_memory_tier_column_degrades_to_unfiltered`
        above, but for `HAS_DESCRIPTION_COLUMN` instead of
        `HAS_TIER_COLUMN` -- a DB built before `description` existed must
        still push a NAME-ONLY bullet (not zero bullets, and not an
        `OperationalError` the hook's own `2>/dev/null || echo ""` would
        otherwise swallow into a silent empty push).
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")

        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        db_path = cache_dir / "wiki-index.db"

        build_script = """
import sqlite3, sys

conn = sqlite3.connect(sys.argv[1])
conn.execute(
    'CREATE VIRTUAL TABLE wiki USING fts5'
    '(filename, name, tags, aliases, audience UNINDEXED, '
    'type UNINDEXED, memory_tier UNINDEXED, '
    'tokenize="porter unicode61")'
)
conn.execute(
    "INSERT INTO wiki VALUES (?,?,?,?,?,?,?)",
    (
        "nodesc-page.md",
        "Nodesccolumntest Page",
        "nodesccolumntest",
        "",
        "|",
        "person",
        "hot",
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
        (cache_dir / "config.env").write_text("AUTO_RECALL=true\nSEARCH_BACKEND=fts5\n")

        result = self._run_hook(hook_env, "tell me about nodesccolumntest")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, (
            "a DB predating the `description` column must degrade to a "
            "name-only push, not silently return zero recall"
        )
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Nodesccolumntest Page" in context
        assert "—" not in context, "no column to render a description from"

    def test_vector_backend_renders_description_not_bare_name(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """AC 'the vector branch renders identically' / required test 6:
        a hit surfaced under `SEARCH_BACKEND=vector` must get the SAME
        `name — description` bullet an FTS5 hit would, resolved from the
        SAME bounded `VECTOR_META` lookup that already carries
        `audience`/`memory_tier` through for a vector-sourced item (issue
        athenaeum#1343). Counter-example this guards against: a bare name
        here (no ` — `) would mean the vector branch fell back to an
        empty description while the FTS5 branch renders enriched
        bullets -- the two backends silently disagreeing.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "hot-vecdesctest.md").write_text(
            "---\n"
            "name: Vecdesctest Hot Page\n"
            "tags: [vecdesctest]\n"
            "description: A vector-sourced hot page about vecdesctest devices\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Unrelated body text, not matched by the probe query below.\n"
        )
        self._seed_index(hook_env)

        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        (cache_dir / "wiki-vectors").mkdir(parents=True, exist_ok=True)
        config_env = cache_dir / "config.env"
        config_env.write_text(
            config_env.read_text().replace(
                "SEARCH_BACKEND=fts5", "SEARCH_BACKEND=vector"
            )
        )

        fake_pkg = tmp_path / "fake-vector-src" / "src" / "athenaeum"
        fake_pkg.mkdir(parents=True)
        vector_env = dict(hook_env)
        vector_env["ATHENAEUM_SRC"] = str(fake_pkg.parent.parent)
        (fake_pkg / "search.py").write_text(
            "def query_vector_index(query, cache_dir, n=3, exclude=None):\n"
            "    exclude = exclude or set()\n"
            "    hits = [('hot-vecdesctest.md', 'Vecdesctest Hot Page', 0.9)]\n"
            "    return [h for h in hits if h[0] not in exclude][:n]\n"
        )

        probe_prompt = "zzznonmatchingzzz term completely unrelated content"
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=json.dumps(
                {"prompt": probe_prompt, "session_id": f"test-{uuid.uuid4().hex}"}
            ),
            env=vector_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected a vector hit to surface"
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert (
            "Vecdesctest Hot Page — A vector-sourced hot page about "
            "vecdesctest devices" in context
        )

    def test_token_cost_increases_with_description(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC: descriptions make `token_cost` go up vs. the same push
        without them -- the awk budget pass must price the WIDER bullet,
        not the bare name (the exact regression the brief's "ledger
        silently keeps reporting the old name-only figure" warning is
        about).
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "bare-costtest.md").write_text(
            "---\n"
            "name: Barecosttest Page\n"
            "tags: [barecosttest]\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Body text about barecosttest, not indexed.\n"
        )
        (wiki / "rich-costtest.md").write_text(
            "---\n"
            "name: Richcosttest Page\n"
            "tags: [richcosttest]\n"
            "description: A substantially longer description text that adds "
            "real weight to the rendered bullet for richcosttest\n"
            "memory_tier: hot\n"
            "---\n\n"
            "Body text about richcosttest, not indexed.\n"
        )
        self._seed_index(hook_env)

        bare_sid = f"bare-{uuid.uuid4().hex}"
        rich_sid = f"rich-{uuid.uuid4().hex}"
        bare_result = self._run_hook(hook_env, "tell me about barecosttest", bare_sid)
        rich_result = self._run_hook(hook_env, "tell me about richcosttest", rich_sid)
        assert bare_result.returncode == 0, f"stderr: {bare_result.stderr}"
        assert rich_result.returncode == 0, f"stderr: {rich_result.stderr}"

        wiki_root = wiki
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        by_session = {r["session_id"]: r for r in records}
        assert bare_sid in by_session and rich_sid in by_session
        bare_cost = by_session[bare_sid]["items"][0]["token_cost"]
        rich_cost = by_session[rich_sid]["items"][0]["token_cost"]
        assert rich_cost > bare_cost, (
            f"description must increase token_cost: bare={bare_cost} rich={rich_cost}"
        )

    def test_over_budget_description_set_is_skipped_not_truncated(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC: 'a deliberately over-long set is truncated by the budget
        rather than pushed' -- with descriptions in play, a budget sized
        to afford fewer than all matching candidates must SKIP the excess
        candidate(s) (the existing greedy-pack behaviour, unchanged by
        this issue), not truncate a candidate's bullet text to fit. A
        name-only control over the SAME budget proves the skip is caused
        by the wider, description-priced bullet specifically.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        long_desc = "A " + ("substantially " * 12) + "long description for budgetdesctest devices"
        for i in range(3):
            (wiki / f"budgetdesctest-{i}.md").write_text(
                "---\n"
                f"name: Budgetdesctest Page {i}\n"
                "tags: [budgetdesctest]\n"
                f"description: {long_desc}\n"
                "memory_tier: hot\n"
                "---\n\n"
                "Body text about budgetdesctest, not indexed.\n"
            )
        for i in range(3):
            (wiki / f"budgetctrltest-{i}.md").write_text(
                "---\n"
                f"name: Budgetctrltest Page {i}\n"
                "tags: [budgetctrltest]\n"
                "memory_tier: hot\n"
                "---\n\n"
                "Body text about budgetctrltest, not indexed.\n"
            )
        self._seed_index(hook_env)

        env = dict(hook_env)
        env["ATHENAEUM_PUSH_TOKEN_BUDGET"] = "70"

        desc_sid = f"desc-{uuid.uuid4().hex}"
        ctrl_sid = f"ctrl-{uuid.uuid4().hex}"
        desc_result = self._run_hook(env, "tell me about budgetdesctest", desc_sid)
        ctrl_result = self._run_hook(env, "tell me about budgetctrltest", ctrl_sid)
        assert desc_result.returncode == 0, f"stderr: {desc_result.stderr}"
        assert ctrl_result.returncode == 0, f"stderr: {ctrl_result.stderr}"

        wiki_root = wiki
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        by_session = {r["session_id"]: r for r in records}
        desc_pushed = by_session[desc_sid]["pushed_count"] if desc_sid in by_session else 0
        ctrl_pushed = by_session[ctrl_sid]["pushed_count"] if ctrl_sid in by_session else 0

        assert desc_pushed < 3, (
            "a budget sized below all-3-candidates-with-descriptions must "
            f"skip at least one candidate rather than push all 3 -- got {desc_pushed}"
        )
        assert ctrl_pushed > desc_pushed, (
            "the SAME budget over a name-only control must afford strictly "
            f"more candidates than the description-bearing set: "
            f"control={ctrl_pushed} description={desc_pushed}"
        )

    def test_no_tier_ranking_term_introduced(self) -> None:
        """AC 'ordering and selection are by relevance alone': every
        `ORDER BY` clause in the hook must be exactly `ORDER BY rank`
        (BM25) -- a structural guard (mirroring
        `test_scope_from_audience_has_no_bash4_only_array_syntax`'s
        approach of asserting directly against the shipped source) so a
        future edit that slips a tier/type term into the ordering, or
        adds a second ranking expression, fails this test even if no
        fixture happens to exercise the difference.
        """
        # Whole-line matches only (the actual SQL clauses each sit alone
        # on their own line inside the heredocs) -- excludes prose
        # mentions of "ORDER BY rank" in surrounding `#` comments, which
        # would otherwise false-positive this structural guard.
        lines = USER_PROMPT.read_text().splitlines()
        order_by_lines = [
            ln.strip()
            for ln in lines
            if ln.strip().startswith("ORDER BY") and not ln.strip().startswith("#")
        ]
        assert len(order_by_lines) >= 2, (
            "expected an ORDER BY clause in both the FTS5 tier and "
            "no-tier branches"
        )
        for clause in order_by_lines:
            assert clause == "ORDER BY rank", f"unexpected ordering term: {clause!r}"


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
