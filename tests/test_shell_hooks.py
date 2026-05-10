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


def _require(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not available on this runner")


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

    (wiki / "lean-startup.md").write_text(
        "---\n"
        "name: Lean Startup\n"
        "tags: [methodology]\n"
        "description: Build-measure-learn methodology\n"
        "---\n\n"
        "The Lean Startup methodology emphasizes rapid iteration and customer feedback.\n"
    )
    (wiki / "customer-development.md").write_text(
        "---\n"
        "name: Customer Development\n"
        "tags: [methodology]\n"
        "description: Steve Blank's four-step framework\n"
        "---\n\n"
        "Customer Development is Steve Blank's framework for startup discovery.\n"
    )

    (knowledge / "athenaeum.yaml").write_text(
        "auto_recall: true\nsearch_backend: fts5\n"
    )

    athenaeum_src = Path(__file__).parent.parent

    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "KNOWLEDGE_ROOT": str(knowledge),
        "ATHENAEUM_SRC": str(athenaeum_src),
        "ATHENAEUM_PYTHON": sys.executable,
    }
    return env


class TestSessionStartRecall:
    def test_builds_fts5_index(self, hook_env: dict[str, str], tmp_path: Path) -> None:
        _require("bash")
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

    def test_exits_clean_when_wiki_missing(self, tmp_path: Path) -> None:
        _require("bash")
        env = {
            "HOME": str(tmp_path),
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
            "is silently ignored. See issue #39."
        )
        hook_output = payload["hookSpecificOutput"]
        assert hook_output.get("hookEventName") == "UserPromptSubmit"
        assert "Customer Development" in hook_output["additionalContext"]

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


class TestPreCompactSave:
    def test_emits_system_message_json(self, tmp_path: Path) -> None:
        _require("bash")
        env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
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

    def test_silent_when_no_pending_file(
        self, hook_env: dict[str, str]
    ) -> None:
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
