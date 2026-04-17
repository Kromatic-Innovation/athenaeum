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
            env=hook_env, capture_output=True, text=True, timeout=30,
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
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0


class TestUserPromptRecall:
    def _seed_index(self, hook_env: dict[str, str]) -> None:
        subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env, capture_output=True, text=True, timeout=30, check=True,
        )

    def test_returns_wiki_match_as_additional_context(
        self, hook_env: dict[str, str]
    ) -> None:
        _require("bash")
        _require("jq")
        _require("sqlite3")
        self._seed_index(hook_env)

        stdin_payload = json.dumps({
            "prompt": "Tell me about customer development frameworks",
            "session_id": f"test-{uuid.uuid4().hex}",
        })
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload, env=hook_env,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected additionalContext JSON on stdout"

        payload = json.loads(result.stdout)
        assert "additionalContext" in payload
        assert "Customer Development" in payload["additionalContext"]

    def test_silent_on_short_prompt(self, hook_env: dict[str, str]) -> None:
        _require("bash")
        _require("jq")
        _require("sqlite3")
        self._seed_index(hook_env)

        stdin_payload = json.dumps({
            "prompt": "hi",
            "session_id": f"test-{uuid.uuid4().hex}",
        })
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload, env=hook_env,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_exits_clean_with_no_index(self, hook_env: dict[str, str]) -> None:
        _require("bash")
        _require("jq")
        stdin_payload = json.dumps({
            "prompt": "anything at all with enough characters",
            "session_id": f"test-{uuid.uuid4().hex}",
        })
        result = subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=stdin_payload, env=hook_env,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout == ""


class TestPreCompactSave:
    def test_emits_system_message_json(self, tmp_path: Path) -> None:
        _require("bash")
        env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
        result = subprocess.run(
            ["bash", str(PRE_COMPACT)],
            env=env, capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert "systemMessage" in payload
        assert "Knowledge checkpoint" in payload["systemMessage"]
