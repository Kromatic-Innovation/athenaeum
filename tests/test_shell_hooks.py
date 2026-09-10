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
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from athenaeum import killswitch
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


# -- issue athenaeum#1513: realistic-tier-mix corpus ------------------------
#
# A fixture that is all-hot or all-warm CANNOT demonstrate tier
# substitution and does not satisfy athenaeum#1345's / athenaeum#1513's
# acceptance criteria. This corpus mirrors the real one's shape: the
# overwhelming majority warm, `hot` confined to `principle` (the live
# hot pool is `principle` 770 / `preference` 92 / `auto-memory` 30 and
# nothing else — zero of 17,265 `person` pages).
#
# It is also deliberately RANK-DISCRIMINATING. For `TIER_MIX_PROMPT` the
# true BM25 top-3 are the three warm `person` pages (the term hits their
# `name`, `tags` AND `description` — three short columns); the two hot
# `principle` pages match only via one long `aliases` list, so they rank
# far below. Measured on this fixture: persons at rank -2.40, principles
# at -0.44. That gap is what makes before/after legible — with the gate
# the query returns the two hot principles (silent substitution, the
# failure mode athenaeum#1345 measured in 10 of 12 sampled queries),
# without it the true top-3 persons.
TIER_MIX_PROMPT = "what do we know about sonderling"

TIER_MIX_PERSON_NAMES = ["Ada Sonderling", "Bram Sonderling", "Cleo Sonderling"]
TIER_MIX_PRINCIPLE_NAMES = ["Governance Principle 0", "Governance Principle 1"]

_TIER_MIX_FILLER_ALIASES = ", ".join(f"filler-alias-{i}" for i in range(30))


def _seed_realistic_tier_mix(wiki: Path) -> None:
    """Write the athenaeum#1513 corpus into *wiki* (see the block comment)."""
    for i, who in enumerate(TIER_MIX_PERSON_NAMES):
        (wiki / f"person-sonderling-{i}.md").write_text(
            "---\n"
            f"name: {who}\n"
            "type: person\n"
            "tags: [sonderling]\n"
            "description: Sonderling protocol lead\n"
            "---\n\nBody text is not indexed; matches come from frontmatter.\n"
        )
    for i, title in enumerate(TIER_MIX_PRINCIPLE_NAMES):
        (wiki / f"principle-hot-{i}.md").write_text(
            "---\n"
            f"name: {title}\n"
            "type: principle\n"
            "tags: [governance]\n"
            f"aliases: [{_TIER_MIX_FILLER_ALIASES}, sonderling]\n"
            "description: A governance principle\n"
            "memory_tier: hot\n"
            "---\n\nBody text is not indexed; matches come from frontmatter.\n"
        )
    for i in range(20):
        (wiki / f"tiermix-filler-{i}.md").write_text(
            "---\n"
            f"name: Filler Page {i}\n"
            "type: company\n"
            "tags: [unrelated]\n"
            "description: nothing relevant here\n"
            "---\n\nBody text is not indexed; matches come from frontmatter.\n"
        )


def _index_db(hook_env: dict[str, str]) -> Path:
    return Path(hook_env["ATHENAEUM_CACHE_DIR"]) / "wiki-index.db"


def _pushed_names(result: subprocess.CompletedProcess[str]) -> set[str]:
    """The page names the hook actually rendered into additionalContext.

    Identity, not count — athenaeum#1345 AC: "a test written as 'still
    returns 3 results' passes the bug, that is exactly what the gate does
    in 10 of 12 cases."
    """
    if not result.stdout:
        return set()
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    return {
        name
        for name in TIER_MIX_PERSON_NAMES + TIER_MIX_PRINCIPLE_NAMES
        if name in context
    }


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

    # `memory_tier: hot` was originally pinned here (issue athenaeum#1120)
    # because user-prompt-recall.sh filtered `WHERE memory_tier = 'hot'`.
    # That gate is gone (issues athenaeum#1345, athenaeum#1513) so the pins
    # are no longer load-bearing for reachability; they are kept because
    # several tests below assert on the tier these pages report in
    # telemetry, and because changing them would churn every consumer of
    # this shared fixture for no behavioural gain. Tests that need a
    # realistic tier MIX build their own corpus via
    # `_seed_realistic_tier_mix` instead — an all-hot fixture like this
    # one cannot demonstrate tier substitution.
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

    # -- issue athenaeum#1513: the hot-tier gate is gone ---------------------

    def test_warm_page_is_no_longer_excluded_by_tier(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issues athenaeum#1345 / athenaeum#1513 — the minimal inversion of
        the old `test_hot_tier_page_surfaces_non_hot_page_excluded`. A
        `hot` page and a `warm` page both match the same query through a
        real index build; BOTH must now surface. Under the gate the warm
        one was silently dropped.
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
        assert "Widgetronic Warm Page" in context, (
            "a warm page matching the query must no longer be excluded by "
            "tier -- the hot-tier gate is removed (athenaeum#1345, "
            f"athenaeum#1513). context={context!r}"
        )

    def test_index_carries_no_tier_column_so_no_gate_is_expressible(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issue athenaeum#1514 AC: "`memory_tier` no longer appears as a
        tier axis in retrieval, ranking, or selection anywhere."

        This replaces athenaeum#1345's `test_fixture_index_actually_
        discriminates_gated_vs_ungated`, which built a hot/warm-mixed
        fixture and proved a gated query returned a DIFFERENT set from an
        ungated one — the "before" half that made
        `test_gate_removal_returns_the_true_bm25_top3` falsifiable.

        That proof is no longer constructible, and its absence is the
        point: with the column gone from the schema, `AND memory_tier =
        'hot'` is not a weaker filter, it is a `sqlite3.OperationalError`.
        So the guard is inverted — instead of demonstrating the gate could
        discriminate, assert that no query CAN name the axis, which is a
        strictly stronger statement and cannot silently lapse the way a
        fixture-dependent one could.
        """
        import sqlite3

        _require("bash")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        _seed_realistic_tier_mix(Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki")
        self._seed_index(hook_env)

        conn = sqlite3.connect(_index_db(hook_env))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(wiki)")}
            assert "memory_tier" not in cols, (
                f"the retired tier axis is back in the index schema: {sorted(cols)}"
            )
            with pytest.raises(sqlite3.OperationalError):
                conn.execute(
                    "SELECT name FROM wiki WHERE wiki MATCH '\"sonderling\"' "
                    "AND memory_tier = 'hot'"
                ).fetchall()
            ungated = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM wiki WHERE wiki MATCH '\"sonderling\"' "
                    "ORDER BY rank LIMIT 3"
                )
            ]
        finally:
            conn.close()

        assert set(ungated) == set(TIER_MIX_PERSON_NAMES), (
            "the true BM25 top-3 on this corpus is the three person pages; "
            f"got {ungated}"
        )


    def test_gate_removal_returns_the_true_bm25_top3(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issues athenaeum#1345 / athenaeum#1513 — the AFTER half, driven
        through the REAL hook end to end.

        Asserts hit IDENTITY, not hit count: the three warm `person`
        pages that are the true BM25 top-3 are pushed, and neither hot
        `principle` substitute is. A "still returns 3 results" assertion
        would pass the bug — the gate returns three hits too, just the
        wrong ones.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        _seed_realistic_tier_mix(Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki")
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, TIER_MIX_PROMPT)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected hookSpecificOutput JSON on stdout"

        assert _pushed_names(result) == set(TIER_MIX_PERSON_NAMES), (
            "the hook must push the true BM25 top-3 (warm `person` pages), "
            "not the hot `principle` substitutes the gate used to backfill "
            f"with. context={result.stdout!r}"
        )

    def test_swapping_two_pages_memory_tier_does_not_change_the_push(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issue athenaeum#1345 AC: "swapping two pages' `memory_tier`
        values in the index does not change which of them is pushed for a
        given query. This is the criterion that catches a soft nudge,
        which no grep will find."

        `memory_tier` is UNINDEXED, so a swap cannot move BM25 — which is
        precisely why any change in the pushed set would have to come
        from a tier term in selection, ordering, or scoring.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        _seed_realistic_tier_mix(wiki)
        self._seed_index(hook_env)
        before = _pushed_names(self._run_hook(hook_env, TIER_MIX_PROMPT))
        assert before, "baseline run pushed nothing -- fixture is broken"

        # Swap: one warm `person` page becomes hot, one hot `principle`
        # page becomes warm. Nothing else changes.
        person = wiki / "person-sonderling-0.md"
        person.write_text(
            person.read_text().replace("type: person\n", "type: person\nmemory_tier: hot\n")
        )
        principle = wiki / "principle-hot-0.md"
        principle.write_text(
            principle.read_text().replace("memory_tier: hot\n", "memory_tier: warm\n")
        )
        self._seed_index(hook_env)

        after = _pushed_names(self._run_hook(hook_env, TIER_MIX_PROMPT))
        assert after == before, (
            "swapping two pages' memory_tier changed which pages were "
            "pushed -- tier is influencing selection or ranking, which "
            f"athenaeum#1345 forbids. before={sorted(before)} "
            f"after={sorted(after)}"
        )

    def test_cold_and_refused_boundaries_survive_the_tier_retirement(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """Issue athenaeum#1514 AC3/AC4: the `cold` and `refused`
        boundaries are PRESERVED and are now named for their real
        mechanisms — storage-surface policy and the never-ingest gate.

        Both counter-examples the issue names are asserted here:

        * AC3 — "a `pii`-class page must still be absent from the index
          and from `recall` after the change; a test asserting only that
          'warm pages still appear' does not satisfy this."
        * AC4 — "content that the never-ingest gate refuses must still
          never be written."

        The vocabulary this test used to reach for is gone, and that is
        the substance of the change rather than an obstacle to testing it:
        `cold` was never enforced by a tier value, it was enforced by
        `storage.is_embedded` (class+config), and `refused` was never
        enforced by a tier value either, it was enforced by
        `never_ingest.classify_never_ingest` upstream of storage. So each
        assertion below now names the mechanism that actually does the
        work, which is what makes this test survive the retirement instead
        of dying with it.
        """
        import sqlite3

        from athenaeum.authority import CLASS_PENDING_STATE_TODO, AuthorityManifest
        from athenaeum.never_ingest import classify_never_ingest
        from athenaeum.search import FTS5Backend
        from athenaeum.storage import is_embedded

        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        # 1. AC3 mechanism — the `cold` boundary is `storage.is_embedded`,
        #    a class+config decision, and it produces NO index row at all.
        cold_config = {"storage": {"mapping": {"credential": "excluded"}}}
        assert is_embedded("credential", cold_config) is False
        assert is_embedded("person", cold_config) is True

        cold_root = tmp_path / "cold-wiki"
        cold_root.mkdir()
        (cold_root / "secret-sonderling.md").write_text(
            "---\n"
            "name: Sonderling Secret\n"
            "type: credential\n"
            "tags: [sonderling]\n"
            "description: Sonderling protocol lead\n"
            "---\n\nbody\n"
        )
        cold_cache = tmp_path / "cold-cache"
        cold_cache.mkdir()
        FTS5Backend().build_index(cold_root, cold_cache, config=cold_config)
        conn = sqlite3.connect(cold_cache / "wiki-index.db")
        try:
            cold_rows = conn.execute(
                "SELECT name FROM wiki WHERE wiki MATCH '\"sonderling\"'"
            ).fetchall()
        finally:
            conn.close()
        assert cold_rows == [], (
            "a non-embedded (cold) class must never enter the index "
            f"(athenaeum#532 H4); got {cold_rows}"
        )

        # 2. AC4 mechanism — the `refused` boundary is `never_ingest`, and
        #    it refuses UPSTREAM of storage: refused content is never
        #    written, so there is nothing downstream to exclude. The
        #    "never written" half is asserted against the real write path
        #    in `tests/test_never_ingest.py`
        #    (`test_refused_file_never_deleted_and_no_wiki_page_written`,
        #    unchanged by this issue); what is pinned HERE is that the
        #    gate is still the thing making that call, now that no tier
        #    value shadows it.
        manifest = AuthorityManifest(
            version=1,
            sources=(),
            never_ingest_classes=(CLASS_PENDING_STATE_TODO,),
        )
        assert (
            classify_never_ingest(
                {"name": "Sonderling rollout", "pending_state": True},
                "body",
                manifest=manifest,
            )
            is not None
        ), "never-ingest must still refuse a declared never-ingest class"
        # The negative control matters: a gate that refused EVERYTHING
        # would satisfy the assertion above vacuously.
        assert (
            classify_never_ingest(
                {"name": "Ada Sonderling", "type": "person"},
                "Sonderling protocol lead.",
                manifest=manifest,
            )
            is None
        ), "never-ingest must not refuse ordinary content"

        # 3. AC3 end to end — a `pii: true` page stays absent from BOTH
        #    the index and the hook's rendered recall, on the SAME run
        #    that proves ordinary pages are reachable. Asserting only the
        #    latter is the counter-example the AC rules out.
        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        _seed_realistic_tier_mix(wiki)
        (wiki / "pii-sonderling.md").write_text(
            "---\n"
            "name: Sonderling Private Dossier\n"
            "type: person\n"
            "tags: [sonderling]\n"
            "description: Sonderling protocol lead\n"
            "pii: true\n"
            "---\n\nbody\n"
        )
        self._seed_index(hook_env)

        conn = sqlite3.connect(_index_db(hook_env))
        try:
            pii_rows = conn.execute(
                "SELECT name FROM wiki WHERE filename = 'pii-sonderling'"
            ).fetchall()
        finally:
            conn.close()
        assert pii_rows == [], f"a pii-flagged page must not be indexed: {pii_rows}"

        result = self._run_hook(hook_env, TIER_MIX_PROMPT)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Sonderling Private Dossier" not in context
        assert _pushed_names(result) == set(TIER_MIX_PERSON_NAMES), (
            "ordinary pages must still be reachable, so this test can fail "
            f"if an exclusion is over-applied. context={context!r}"
        )


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

    def test_vector_backend_surfaces_every_page_and_records_no_tier(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """Issues athenaeum#1345 / athenaeum#1513 -- the inversion of the
        old `test_vector_backend_hot_tier_filter_drops_non_hot_hit`, and
        the surface that MATTERS: live traffic is 100% `backend:
        "vector"` (32 of 32 sampled sidecar pushes), so a fix that
        removed only the FTS5 `WHERE` would have changed nothing
        observable while looking green. athenaeum#1420 was closed
        precisely because retrieval tests never exercised this backend.

        Both fixture pages must surface through the vector path (the two
        are pinned to the old `hot`/`warm` frontmatter values purely so
        this test keeps exercising what used to be the discriminating
        case -- proving the change is not a no-op that eats every vector
        hit). Issue athenaeum#1514 additionally retired the vocabulary, so
        `VECTOR_META` no longer carries a tier and vector telemetry
        records no `memory_tier` key at all -- pinned below, since a
        vector-sourced row is exactly where a reintroduced tier join would
        show up first.

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
        assert warm_result.stdout, (
            "a warm-pinned vector hit must surface -- the vector "
            "enforcement surface (the hot-only VECTOR_META lookup and the "
            "awk keep-filter it fed) is removed (athenaeum#1345, "
            "athenaeum#1513)"
        )
        warm_context = json.loads(warm_result.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        assert "Vectester Warm Page" in warm_context, f"got: {warm_context!r}"

        # Telemetry: `VECTOR_META` is still the metadata join (audience
        # and description), but issue athenaeum#1514 dropped the tier
        # column from it along with the vocabulary, so a vector-sourced
        # item carries NO `memory_tier` key. This is the narrowest place
        # the join's width is observable end to end, which is why the
        # assertion lives here rather than only in the FTS5 tests.
        warm_records = read_push_records(
            wiki_root=Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki",
            cache_dir=Path(hook_env["ATHENAEUM_CACHE_DIR"]),
        )
        assert warm_records, "expected a sidecar push record for the warm hit"
        warm_items = [it for rec in warm_records for it in rec["items"]]
        assert warm_items and all(it["backend"] == "vector" for it in warm_items)
        assert all("memory_tier" not in it for it in warm_items), (
            f"the retired tier vocabulary is back in vector telemetry: {warm_items}"
        )
        # The join itself must still be intact — a `VECTOR_META` lookup
        # that silently returned nothing would also satisfy the assertion
        # above. `scope` is derived from the joined `audience` column.
        assert all(it["scope"] for it in warm_items), (
            f"VECTOR_META's audience join must still populate scope: {warm_items}"
        )

        # Same stub, the hot-pinned page instead -- proves the change
        # isn't a no-op that happens to eat every vector hit.
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
        assert hot_result.stdout, "expected the hot-pinned vector hit to surface"
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
        hook-written row unmodified. Also covers athenaeum#1513's
        telemetry AC: each pushed page's `memory_tier` is recorded and is
        TRUTHFUL -- it equals the tier the fixture page actually carries,
        not the constant `"hot"` the removed gate made it by
        construction. This is the field that evidences the mix shifting
        off 3.5% hot.
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
        # Issue athenaeum#1514 retired the retrieval-cost vocabulary, so a
        # hook-written row carries NO `memory_tier` key at all. Absence is
        # the contract, not an empty string: `athenaeum.push_metrics` reads
        # an absent key as "written after the retirement", which an empty
        # value could not be distinguished from. The fixture page still
        # carries the orphaned frontmatter key -- nothing reads it, and
        # nothing rewrote the corpus to remove it (that is this issue's
        # stated frontmatter migration).
        pushed_page = (
            Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki" / "customer-development.md"
        )
        assert "memory_tier: hot" in pushed_page.read_text()
        assert "memory_tier" not in item, (
            "the retired tier vocabulary must not reappear in telemetry: "
            f"{item}"
        )

    def test_telemetry_records_no_tier_for_the_realistic_mix(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issue athenaeum#1514 AC2, on the surface athenaeum#1513 used as
        its evidence channel.

        athenaeum#1513's AC was "telemetry still records each pushed
        page's `memory_tier`, so the mix shifting off 3.5% hot is
        observable" — deliberately kept alive so the shift could be
        measured. It WAS measured (athenaeum#1560: the sidecar path went
        from 98 hot / 0 warm before the fix's deploy to 66 hot / 1824
        warm after), which is what unblocked this issue. With the evidence
        gathered, the field is retired rather than left recording a
        vocabulary nothing else uses.

        The push itself is still asserted by identity — the three warm
        `person` pages, not a count — so this cannot pass on a hook that
        stopped pushing anything at all.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        _seed_realistic_tier_mix(Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki")
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, TIER_MIX_PROMPT)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert _pushed_names(result) == set(TIER_MIX_PERSON_NAMES)

        records = read_push_records(
            wiki_root=Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki",
            cache_dir=Path(hook_env["ATHENAEUM_CACHE_DIR"]),
        )
        items = [it for rec in records for it in rec["items"]]
        assert len(items) == 3, f"expected the three person pages: {items}"
        assert all("memory_tier" not in it for it in items), (
            f"the retired tier vocabulary is back in telemetry: {items}"
        )

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

    def test_hook_never_names_a_wiki_root_ledger_path(self) -> None:
        """Issue athenaeum#1591, mechanical guard on the LIVE producer. This
        hook resolves the ledger path in bash, independently of
        `push_metrics.durable_push_records_path`, and it is what actually
        migrated the operator's deployment into the corpus. A future edit that
        reintroduces a wiki-root branch here would be invisible to every
        Python-side test, so assert on the script text itself.
        """
        text = USER_PROMPT.read_text(encoding="utf-8")
        offenders = [
            line
            for line in text.splitlines()
            if "_push_records.jsonl" in line
            and not line.lstrip().startswith("#")
            and "PM_CACHE_DIR" not in line
        ]
        assert offenders == [], f"ledger path must resolve under the cache dir only: {offenders}"

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

    def test_ledger_path_cache_dir_when_neither_file_present(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC (issue athenaeum#1591): a tmpdir with NEITHER file present --
        both the hook and `durable_push_records_path` resolve to the CACHE
        DIR, and nothing appears under `<wiki_root>`. This previously
        asserted the opposite (the wiki-root "new" branch); the relocation it
        encoded is withdrawn. Paired with the legacy-branch test above --
        that case alone would pass vacuously and prove nothing.
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

        assert durable_push_records_path(wiki_root, cache_dir=cache_dir) == legacy_path

        result = self._run_hook(hook_env, "tell me about customer development frameworks")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        assert legacy_path.is_file()
        assert not new_path.exists(), "must not write telemetry into the wiki corpus"
        assert durable_push_records_path(wiki_root, cache_dir=cache_dir) == legacy_path

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
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        # Issue athenaeum#1591: cache dir, not wiki root.
        ledger_path = durable_push_records_path(wiki_root, cache_dir=cache_dir)

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

    # ── issue athenaeum#1516: multi-line `-v` is a hard recall outage ──
    #
    # `VECTOR_META` is one tab-separated row per matched filename. The
    # hook used to hand it to awk as `-v meta="$VECTOR_META"`. BWK awk
    # (`/usr/bin/awk` on macOS -- the deployed interpreter) REJECTS a
    # `-v` assignment containing a newline outright, exits 2, and emits
    # nothing; gawk accepts it. So the bug was invisible on a gnu-awk CI
    # runner AND invisible in production for as long as the hot-tier gate
    # existed, because at ~3.5% hot a vector query almost never returned
    # two or more *hot* metadata rows -- `meta` was empty or exactly one
    # line, the one shape that works under both awks. Removing the gate
    # (athenaeum#1513) unmasked it.
    #
    # Hence the two properties every test below is built around:
    #   1. MULTI-ROW metadata. A zero- or one-row fixture is exactly the
    #      shape that passed for the gate's entire lifetime and cannot
    #      reproduce this.
    #   2. BWK-AWK SEMANTICS. `_awk_shim_dir` supplies them on any
    #      runner, so the guard does not quietly evaporate on a box where
    #      `awk` is gawk.

    _MULTI_ROW_PAGES = (
        (
            "multirowawk-alpha.md",
            "Multirowawk Alpha",
            "First of three multirowawk metadata rows",
            "hot",
        ),
        (
            "multirowawk-beta.md",
            "Multirowawk Beta",
            "Second of three multirowawk metadata rows",
            "warm",
        ),
        (
            "multirowawk-gamma.md",
            "Multirowawk Gamma",
            "Third of three multirowawk metadata rows",
            "cold",
        ),
    )

    def _seed_multi_row_vector(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> dict[str, str]:
        """Seed THREE wiki pages and stub the vector backend to return all
        three, so the hook's ``VECTOR_META`` lookup is genuinely
        multi-line.

        Seeding three *pages* (not merely stubbing three *hits*) is the
        load-bearing part: ``VECTOR_META`` comes from ``SELECT ... FROM
        wiki WHERE filename IN (...)``, so three hits against one seeded
        page would still yield a single metadata row -- the exact
        one-line shape that never reproduced the bug.

        Deliberately spans hot/warm/cold tiers as well. This is not a
        tier assertion (athenaeum#1345/#1513: no tier predicate may ever
        return); it is a guard that the fix is not accidentally
        reintroducing one, since any tier filter would shrink the
        metadata set back toward the single-row shape that hid the bug.
        """
        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        for filename, name, description, tier in self._MULTI_ROW_PAGES:
            (wiki / filename).write_text(
                "---\n"
                f"name: {name}\n"
                "tags: [multirowawk]\n"
                f"description: {description}\n"
                f"memory_tier: {tier}\n"
                "---\n\n"
                "Unrelated body text, not matched by the probe query.\n"
            )

        # Build the FTS5 index with the checkout's ``src`` on PYTHONPATH.
        # ``hook_env`` isolates HOME, which hides per-user site-packages
        # (PEP 370), so on a developer box where athenaeum is only
        # installed in the user site the index build fails open and
        # leaves NO ``wiki-index.db`` -- which would leave
        # ``VECTOR_META`` empty, i.e. single-line, i.e. the exact shape
        # that never reproduced athenaeum#1516. This test would then pass
        # vacuously on precisely the platform (macOS/BWK awk) whose awk
        # it exists to exercise. On CI, where athenaeum is installed,
        # this is a no-op.
        seed_env = dict(hook_env)
        src = str(Path(hook_env["ATHENAEUM_SRC"]) / "src")
        existing = seed_env.get("PYTHONPATH", "")
        seed_env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
        self._seed_index(seed_env)

        db_file = Path(hook_env["ATHENAEUM_CACHE_DIR"]) / "wiki-index.db"
        assert db_file.exists(), (
            "the FTS5 index did not build; without it VECTOR_META is "
            "empty and this test cannot reproduce athenaeum#1516"
        )

        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        (cache_dir / "wiki-vectors").mkdir(parents=True, exist_ok=True)
        config_env = cache_dir / "config.env"
        config_env.write_text(
            config_env.read_text().replace(
                "SEARCH_BACKEND=fts5", "SEARCH_BACKEND=vector"
            )
        )

        fake_pkg = tmp_path / "fake-multirow-src" / "src" / "athenaeum"
        fake_pkg.mkdir(parents=True)
        hits = [
            (filename, name, 0.9 - 0.1 * i)
            for i, (filename, name, _d, _t) in enumerate(self._MULTI_ROW_PAGES)
        ]
        (fake_pkg / "search.py").write_text(
            "def query_vector_index(query, cache_dir, n=3, exclude=None):\n"
            "    exclude = exclude or set()\n"
            f"    hits = {hits!r}\n"
            "    return [h for h in hits if h[0] not in exclude][:n]\n"
        )

        vector_env = dict(hook_env)
        vector_env["ATHENAEUM_SRC"] = str(fake_pkg.parent.parent)
        return vector_env

    @staticmethod
    def _awk_shim_dir(tmp_path: Path) -> Path:
        """A PATH directory holding an ``awk`` that enforces BWK's refusal
        of a multi-line ``-v`` assignment, then execs the real awk.

        Without this, the regression test is only meaningful on a box
        whose ``awk`` is BWK awk. CI runs on Linux, where ``awk`` is
        gawk, and gawk ACCEPTS a multi-line ``-v`` -- so the unfixed hook
        passes there. That gap is why athenaeum#1516 reached production;
        a test that inherits it proves nothing.

        The shim is a strict subset of BWK's behaviour: it rejects only
        what BWK rejects and otherwise delegates verbatim, so it cannot
        make a passing hook fail for an unrelated reason. Every ``awk``
        call in the hook is a bare, PATH-resolved ``awk`` (verified: no
        absolute path, and the hook never reassigns ``PATH``), so the
        shim covers all of them.
        """
        real_awk = shutil.which("awk")
        assert real_awk, "awk not on PATH"
        shim_dir = tmp_path / "awk-shim"
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim = shim_dir / "awk"
        # `real_awk` is resolved HERE, not inside the shim: a runtime
        # `command -v awk` would find the shim itself and recurse.
        shim.write_text(
            f"""#!/bin/bash
REAL_AWK={shlex.quote(real_awk)}
NL=$'\\n'
expect_v=0
for a in "$@"; do
  if [ "$expect_v" = 1 ]; then
    val="$a"; expect_v=0
  elif [ "$a" = -v ]; then
    expect_v=1; continue
  elif [ "${{a#-v}}" != "$a" ]; then
    val="${{a#-v}}"
  else
    continue
  fi
  case "$val" in
    *"$NL"*)
      printf 'awk: newline in string %s... at source line 1\\n' "${{val:0:20}}" >&2
      exit 2 ;;
  esac
done
exec "$REAL_AWK" "$@"
"""
        )
        shim.chmod(0o755)
        return shim_dir

    def _assert_all_multi_row_bullets(self, result) -> None:
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, (
            "a multi-row vector metadata set must still inject a context "
            "block -- empty stdout here is the athenaeum#1516 outage "
            "(awk exits 2 on a multi-line `-v` and emits nothing)"
        )
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        for _filename, name, description, _tier in self._MULTI_ROW_PAGES:
            assert f"{name} — {description}" in context, (
                f"expected the joined bullet for {name!r} in: {context!r}"
            )

    def test_multi_row_vector_metadata_still_injects_context(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#1516 regression: three vector hits => a three-line
        ``VECTOR_META`` => the hook must STILL emit a context block with
        all three ``name — description`` bullets joined in.

        This runs under whatever ``awk`` the box provides. On macOS (BWK
        awk) it fails outright against the pre-fix ``-v meta=`` form. On
        a gawk box it passes either way -- which is precisely why the
        shim variant below exists; this test is the natural-environment
        half, not the guarantee.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        vector_env = self._seed_multi_row_vector(hook_env, tmp_path)
        result = self._run_hook(
            vector_env, "zzznonmatchingzzz term completely unrelated content"
        )
        self._assert_all_multi_row_bullets(result)

    def test_multi_row_vector_metadata_survives_bwk_awk_semantics(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """The same assertion, forced onto the DEPLOYED awk's semantics on
        every runner via a PATH-prepended shim that refuses a multi-line
        ``-v`` exactly as BWK awk does.

        This is the test that actually holds the line. Without it the
        guard above is vacuous on CI's gnu-awk box -- the same blind spot
        that let athenaeum#1516 ship.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")

        vector_env = self._seed_multi_row_vector(hook_env, tmp_path)
        shim_dir = self._awk_shim_dir(tmp_path)
        vector_env["PATH"] = f"{shim_dir}{os.pathsep}{vector_env['PATH']}"

        result = self._run_hook(
            vector_env, "zzznonmatchingzzz term completely unrelated content"
        )
        assert "newline in string" not in result.stderr, (
            "the hook still hands a multi-line value to `awk -v` "
            f"(athenaeum#1516): {result.stderr!r}"
        )
        self._assert_all_multi_row_bullets(result)

    def test_vector_meta_is_never_passed_through_awk_dash_v(self) -> None:
        """Source guard: ``VECTOR_META`` is multi-line by nature, so it may
        never travel through ``-v`` again (athenaeum#1516). Cheap and
        interpreter-independent -- it holds even on a runner where both
        behavioural tests above skip for a missing ``sqlite3``/``jq``.
        """
        source = USER_PROMPT.read_text()
        assert "-v meta=" not in source, (
            "VECTOR_META must reach awk through awk's own input stream, "
            "not a `-v` assignment: BWK awk rejects a multi-line `-v` "
            "outright and emits nothing (athenaeum#1516)"
        )
        assert '-v preamble="$PREAMBLE" -v budget="$BUDGET"' in source, (
            "the budget pass's two `-v` values are audited single-line "
            "(a static literal and a digits-validated integer); if this "
            "call site changes shape, re-audit it against athenaeum#1516"
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

        There is ONE such clause since issue athenaeum#1514 collapsed the
        hook's tier/no-tier query branches into a single query (the tier
        column it chose between no longer exists).
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
        assert len(order_by_lines) >= 1, (
            "expected an ORDER BY clause in the FTS5 query"
        )
        for clause in order_by_lines:
            assert clause == "ORDER BY rank", f"unexpected ordering term: {clause!r}"

    def test_no_tier_predicate_survives_on_either_surface(self) -> None:
        """Issues athenaeum#1345 / athenaeum#1513 AC: "grepping the
        implementation for `memory_tier` finds it only in
        metadata/telemetry positions, never in a `WHERE`, `ORDER BY`, or
        scoring expression."

        A structural guard beside `test_no_tier_ranking_term_introduced`,
        because the original gate had TWO enforcement surfaces and
        athenaeum#1345 warns that removing only the obvious one
        "reintroduces branch divergence with the sign flipped". Comment
        lines are excluded -- the file deliberately keeps prose about the
        gate it removed.
        """
        offenders = []
        for lineno, raw in enumerate(USER_PROMPT.read_text().splitlines(), start=1):
            line = raw.strip()
            if line.startswith("#") or "memory_tier" not in line:
                continue
            lowered = line.lower()
            if lowered.startswith(("and ", "where ", "order by ", "having ")):
                offenders.append((lineno, line))
            elif "memory_tier" in lowered and "=" in lowered and "'hot'" in lowered:
                offenders.append((lineno, line))
        assert offenders == [], (
            "a tier predicate survives on the push path -- the gate must be "
            f"gone from BOTH the lexical and the vector surface: {offenders}"
        )

        # And the awk keep-filter the vector VECTOR_META lookup used to
        # feed is gone with it (a grep for the SQL alone would miss it).
        assert "_hot_vector_filenames" not in USER_PROMPT.read_text(), (
            "the vector post-filter's derived 'kept' set must be removed "
            "along with the hot-only VECTOR_META restriction"
        )

    # -- issue athenaeum#1530: local topics trace ---------------------------
    #
    # athenaeum#711 decided the ledger stores a query HASH, never raw text or
    # topics -- a deliberate privacy property this issue must not weaken.
    # Topics instead go to a SEPARATE, local, ring-buffered file the viewer
    # joins to a push record by `query_hash`. See that file's own docstring
    # for the two 2026-09-09 incidents (athenaeum#1513, athenaeum#1516) that
    # make "verify by running the hook, not by reading the diff" load-bearing
    # for every test below.

    def _topics_trace_path(self, hook_env: dict[str, str]) -> Path:
        # Mirrors the hook's `PM_CACHE_DIR="${ATHENAEUM_CACHE_DIR:-$HOME/.cache/athenaeum}"`
        # -- NOT the hook's plain (non-overridable) `CACHE_DIR`, which is
        # hardcoded to `${HOME}/.cache/athenaeum` and ignores
        # `ATHENAEUM_CACHE_DIR` entirely. `_cmd_viewer.py`'s
        # `_load_topics_for_query_hash` resolves this SAME file via
        # `athenaeum.config.resolve_cache_dir` (`arg > ATHENAEUM_CACHE_DIR env
        # > default`), i.e. `PM_CACHE_DIR`'s exact shape -- so this helper
        # must match `PM_CACHE_DIR`, not `CACHE_DIR`, or these tests would
        # pass by the two paths coincidentally being equal (as they are
        # whenever `hook_env`'s `ATHENAEUM_CACHE_DIR` happens to already sit
        # under `HOME`) rather than by actually exercising the resolution
        # every deployment that sets `ATHENAEUM_CACHE_DIR` relies on.
        cache_dir = hook_env.get("ATHENAEUM_CACHE_DIR") or str(
            Path(hook_env["HOME"]) / ".cache" / "athenaeum"
        )
        return Path(cache_dir) / "_last_turn_topics.jsonl"

    def _wait_for_topics_row(
        self, trace_path: Path, query_hash: str, timeout: float = 5.0
    ) -> dict[str, Any]:
        """Poll for the backgrounded trace write (AC4's fire-and-forget
        design means it can still be in flight when the hook process, and
        therefore `subprocess.run`, has already returned)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if trace_path.is_file():
                for line in reversed(trace_path.read_text().splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict) and row.get("query_hash") == query_hash:
                        return row
            time.sleep(0.05)
        pytest.fail(
            f"topics trace at {trace_path} never recorded query_hash={query_hash!r} "
            f"within {timeout}s"
        )

    def test_topics_trace_keyed_by_same_query_hash_as_push_record(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC1: after a turn, the trace holds that turn's topics keyed by
        the SAME `query_hash` the push record carries -- asserted by
        joining the two artifacts on that value, not by reading the diff.
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
        query_hash = records[0]["query_hash"]
        assert query_hash == _query_hash(probe)

        row = self._wait_for_topics_row(self._topics_trace_path(hook_env), query_hash)
        assert row["query_hash"] == query_hash
        assert isinstance(row["topics"], list) and row["topics"]
        # The regex fallback extractor (ATHENAEUM_CLI is stubbed to a
        # nonexistent path in `hook_env`) tokenizes the probe itself, so the
        # recorded topics must actually reflect it -- not an empty or
        # unrelated placeholder.
        assert any(t in row["topics"] for t in ("customer", "development", "frameworks"))
        assert probe not in json.dumps(row), (
            "the trace holds extracted topic TOKENS, never the raw prompt text"
        )

    def test_push_record_shape_unchanged_no_topics_key(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC2 (hard gate): push records stay byte-identical in shape --
        pinned by an explicit key-set assertion, not a spot-check, so this
        cannot regress by a future edit adding `topics` (or anything else)
        to the ledger row. athenaeum#711 stays intact: the ledger keeps a
        query HASH only.
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
        assert result.stdout

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        rec = records[0]

        assert set(rec) == {
            "v",
            "session_id",
            "ts",
            "query_hash",
            "backend",
            "items",
            "pushed_count",
            "token_cost",
            "token_cost_estimated",
            "source",
        }, f"push record shape changed: {sorted(rec)}"
        assert "topics" not in rec

        for item in rec["items"]:
            # No `memory_tier`: issue athenaeum#1514 retired the
            # retrieval-cost vocabulary and this writer stopped emitting
            # the key. `tier` here is the unrelated ACCESS tier.
            assert set(item) == {
                "id",
                "tier",
                "scope",
                "token_cost",
                "relevance",
                "backend",
            }, f"push record item shape changed: {sorted(item)}"
            assert "topics" not in item

        raw_line = durable_push_records_path(wiki_root, cache_dir=cache_dir).read_text()
        assert "topics" not in raw_line

    def test_topics_trace_is_ring_buffered_and_bounded(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC3: the trace is bounded (last N turns) and never grows without
        limit. Pre-seeds the file well past a small test-only cap, runs one
        more turn, and asserts the file settles back at the cap rather than
        accumulating unboundedly.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        trace_path = self._topics_trace_path(hook_env)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        max_lines = 5
        with trace_path.open("w") as f:
            for i in range(50):
                f.write(
                    json.dumps(
                        {
                            "session_id": "pre-existing",
                            "ts": "2026-01-01T00:00:00Z",
                            "query_hash": f"deadbeef0000{i:04d}"[:16],
                            "topics": ["filler"],
                        }
                    )
                    + "\n"
                )

        env = dict(hook_env)
        env["ATHENAEUM_TOPICS_TRACE_MAX_LINES"] = str(max_lines)
        probe = "tell me about customer development frameworks"
        result = self._run_hook(env, probe)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        query_hash = records[0]["query_hash"]
        self._wait_for_topics_row(trace_path, query_hash)

        deadline = time.time() + 5.0
        line_count = None
        while time.time() < deadline:
            line_count = len(
                [ln for ln in trace_path.read_text().splitlines() if ln.strip()]
            )
            if line_count <= max_lines:
                break
            time.sleep(0.05)
        assert line_count == max_lines, (
            f"expected the ring buffer to settle at {max_lines} lines after "
            f"trimming, got {line_count}"
        )
        # And the newest row (this turn's) must have survived the trim --
        # a correct ring buffer keeps the TAIL, not an arbitrary N lines.
        kept_hashes = {
            json.loads(ln)["query_hash"]
            for ln in trace_path.read_text().splitlines()
            if ln.strip()
        }
        assert query_hash in kept_hashes

    def test_topics_trace_write_failure_never_affects_injection(
        self, hook_env: dict[str, str]
    ) -> None:
        """AC4 (hard gate): a failed trace write degrades to "no topics
        recorded", NEVER to "no context injected" and never to a slower
        turn. The write is forced to fail for real (the trace path is a
        DIRECTORY, so the hook's own `>>` append cannot succeed) rather
        than asserted only by reading the diff.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        trace_path = self._topics_trace_path(hook_env)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        # A directory where the hook expects to append a file: every write
        # attempt (`printf ... >> "$PM_TOPICS_TRACE_PATH"`) fails with
        # "Is a directory", exercising the REAL failure path rather than a
        # simulated one.
        trace_path.mkdir()

        start = time.monotonic()
        result = self._run_hook(
            hook_env, "tell me about customer development frameworks"
        )
        elapsed = time.monotonic() - start
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, (
            "a trace-write failure must never suppress the injected context"
        )
        payload = json.loads(result.stdout)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Customer Development" in context

        # The synchronous portion of the hook (everything up to and
        # including its stdout) must not be slowed by a doomed trace write
        # -- the write is backgrounded specifically so this holds even if
        # the background attempt itself is slow to fail.
        assert elapsed < 5.0, (
            f"hook took {elapsed:.2f}s; a failing trace write must not slow "
            "the synchronous turn"
        )

        # The push record ledger -- an entirely separate write -- must be
        # completely unaffected by the topics-trace failure.
        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1

        assert trace_path.is_dir(), "the forced-failure fixture itself must be untouched"

    def test_topics_trace_survives_bwk_awk_semantics(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """AC5: this issue's regression test runs under BWK-semantics awk,
        not gawk. `user-prompt-recall.sh` has the worst incident history in
        the repo precisely because a gawk-green CI result proved nothing
        for athenaeum#1516 -- a multi-line `awk -v` value that GNU awk
        accepts outright crashes the deployed BWK awk. This issue's own new
        code (`_pm_topics_json_array`, `_pm_write_topics_trace`) adds no new
        `awk` invocation at all -- it is pure bash -- but the surrounding
        hook still runs several existing `awk` passes on the same turn, and
        this test is the guard that the topics-trace addition did not
        perturb any of them under the DEPLOYED interpreter's semantics.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        shim_dir = self._awk_shim_dir(tmp_path)
        env = dict(hook_env)
        env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"

        probe = "tell me about customer development frameworks"
        result = self._run_hook(env, probe)
        assert "newline in string" not in result.stderr, (
            f"BWK awk semantics broke under this issue's change: {result.stderr!r}"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout

        wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        cache_dir = Path(hook_env["ATHENAEUM_CACHE_DIR"])
        records = read_push_records(wiki_root=wiki_root, cache_dir=cache_dir)
        assert len(records) == 1
        query_hash = records[0]["query_hash"]

        row = self._wait_for_topics_row(self._topics_trace_path(env), query_hash)
        assert row["topics"]

    def test_topics_trace_and_viewer_agree_when_athenaeum_cache_dir_diverges_from_home(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """Regression for a Seer review finding on this PR: the trace path
        was originally built from the hook's plain, hardcoded
        `CACHE_DIR="${HOME}/.cache/athenaeum"` while `_cmd_viewer.py` reads
        the same file via `athenaeum.config.resolve_cache_dir`, whose
        precedence is `arg > ATHENAEUM_CACHE_DIR env > default`. Any
        deployment that actually SETS `ATHENAEUM_CACHE_DIR` would have the
        hook write to one directory and the viewer read from another --
        silently, since a miss renders the pre-existing (and otherwise
        legitimate) "not instrumented" state rather than an error. That is
        exactly the "wrong result that looks like a legitimate one" failure
        mode issue athenaeum#1530 cites athenaeum#1513 for.

        `hook_env`'s own `ATHENAEUM_CACHE_DIR` happens to already sit under
        `HOME`, so every OTHER test in this class would pass even with that
        bug present -- tested by coincidence, not by the join. This test
        points `ATHENAEUM_CACHE_DIR` at a directory that shares NO path
        segment with `HOME`, so the two resolutions can only agree by
        actually consulting the same env var, then asserts the join two
        ways: the file lands where `PM_CACHE_DIR` (not `CACHE_DIR`) resolves
        to, AND `_cmd_viewer._load_topics_for_query_hash` -- the viewer's
        own real production function, not a hand-rolled path -- finds the
        same row when pointed at that same directory.
        """
        _require("bash")
        _require("jq")
        _require("sqlite3")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed_index(hook_env)

        # A standalone `tempfile.mkdtemp()`, deliberately NOT nested under
        # `tmp_path` -- `hook_env` and this test share the same `tmp_path`
        # fixture instance, so anything built from `tmp_path` (including
        # `hook_env["HOME"]`) shares its prefix. Only a directory rooted
        # OUTSIDE that shared tree proves the two resolutions agree by
        # actually consulting `ATHENAEUM_CACHE_DIR`, rather than by both
        # happening to descend from the same fixture.
        divergent_cache = Path(tempfile.mkdtemp(prefix="athenaeum-divergent-cache-"))
        try:
            env = dict(hook_env)
            env["ATHENAEUM_CACHE_DIR"] = str(divergent_cache)
            assert not str(divergent_cache).startswith(str(Path(hook_env["HOME"])))

            probe = "tell me about customer development frameworks"
            result = self._run_hook(env, probe)
            assert result.returncode == 0, f"stderr: {result.stderr}"
            assert result.stdout

            wiki_root = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
            records = read_push_records(wiki_root=wiki_root, cache_dir=divergent_cache)
            assert len(records) == 1
            query_hash = records[0]["query_hash"]

            # 1. The trace file must land under the DIVERGENT
            # `ATHENAEUM_CACHE_DIR` -- not under the hardcoded
            # `$HOME/.cache/athenaeum` the pre-fix code used.
            correct_trace_path = divergent_cache / "_last_turn_topics.jsonl"
            stale_trace_path = (
                Path(hook_env["HOME"]) / ".cache" / "athenaeum" / "_last_turn_topics.jsonl"
            )
            row = self._wait_for_topics_row(correct_trace_path, query_hash)
            assert not stale_trace_path.is_file(), (
                "the trace must not also (or instead) land at the hardcoded "
                "$HOME-derived path when ATHENAEUM_CACHE_DIR diverges from it"
            )

            # 2. The viewer's own real lookup function, pointed at the SAME
            # divergent cache_dir, must find the SAME row -- this is the
            # AC1/AC6 join actually being tested, not merely a
            # file-existence check on each side independently.
            from athenaeum import _cmd_viewer

            topics = _cmd_viewer._load_topics_for_query_hash(
                query_hash, cache_dir=divergent_cache
            )
            assert topics == row["topics"]
            assert topics, "expected a non-empty topics list to have round-tripped"
        finally:
            shutil.rmtree(divergent_cache, ignore_errors=True)


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

    # -- Mixed-case / whitespace-padded ATHENAEUM_DISABLED (athenaeum#1354) --
    #
    # `_env_scope()` in killswitch.py normalizes with `raw.strip().lower()`
    # before matching; the shell copies of the same rule used a bare `case`
    # statement with no normalization, so `TRUE`, `All`, `" true "` etc. were
    # silently ignored by every hook while the Python entry points honoured
    # them. These use `pre-compact-save.sh` as the probe hook because it
    # needs neither a wiki nor a python subprocess to demonstrate "ran
    # normally" vs. "no-opped" — the same `__athenaeum_recall_disabled`
    # helper (copy-pasted verbatim into all six hooks) gates every one.
    # Each case is cross-checked against `killswitch.is_disabled("recall")`
    # given the identical env value, so the hook and the Python reference
    # cannot silently diverge again.

    def _run_pre_compact(self, tmp_path: Path, value: str) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(tmp_path),
            "ATHENAEUM_CACHE_DIR": str(tmp_path / ".cache" / "athenaeum"),
            "PATH": os.environ.get("PATH", ""),
            "ATHENAEUM_DISABLED": value,
        }
        return subprocess.run(
            ["bash", str(PRE_COMPACT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "all"])
    def test_env_override_lowercase_all_scope_still_disables(
        self, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-existing canonical lowercase spellings must keep working unchanged."""
        _require("bash")
        cache_dir = tmp_path / ".cache" / "athenaeum"
        result = self._run_pre_compact(tmp_path, value)
        assert result.returncode == 0
        assert result.stdout == ""

        monkeypatch.setenv("ATHENAEUM_DISABLED", value)
        assert killswitch.is_disabled("recall", cache_dir=cache_dir) is True

    @pytest.mark.parametrize("value", ["TRUE", "All", "ON", "Yes", " true "])
    def test_env_override_mixed_case_and_whitespace_disables(
        self, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mixed-case and whitespace-padded values must disable too, matching
        `killswitch.is_disabled("recall")` for the identical value."""
        _require("bash")
        cache_dir = tmp_path / ".cache" / "athenaeum"
        result = self._run_pre_compact(tmp_path, value)
        assert result.returncode == 0
        assert result.stdout == ""

        monkeypatch.setenv("ATHENAEUM_DISABLED", value)
        assert killswitch.is_disabled("recall", cache_dir=cache_dir) is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "", "maybe"])
    def test_env_override_negative_values_defer_to_state_file(
        self, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counter-examples must NOT be treated as a disable — with no state
        file present, the hook must run normally, matching
        `killswitch.is_disabled("recall")` returning False for each."""
        _require("bash")
        cache_dir = tmp_path / ".cache" / "athenaeum"
        result = self._run_pre_compact(tmp_path, value)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert "Knowledge checkpoint" in payload["systemMessage"]

        monkeypatch.setenv("ATHENAEUM_DISABLED", value)
        assert killswitch.is_disabled("recall", cache_dir=cache_dir) is False

    @pytest.mark.parametrize("value", ["compile", "COMPILE", "Compile"])
    def test_env_override_compile_scope_leaves_recall_running(
        self, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The compile arm of the case statement must be normalized too, not
        just the disable arm — `compile` keeps recall hooks running whatever
        case it's typed in."""
        _require("bash")
        cache_dir = tmp_path / ".cache" / "athenaeum"
        result = self._run_pre_compact(tmp_path, value)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert "Knowledge checkpoint" in payload["systemMessage"]

        monkeypatch.setenv("ATHENAEUM_DISABLED", value)
        assert killswitch.is_disabled("recall", cache_dir=cache_dir) is False

    # The four behavioural tests above probe ONE hook (`pre-compact-save.sh`,
    # the only one that needs neither a wiki nor a python subprocess). That is
    # sound only for as long as all six copies of the helper stay identical —
    # and "six hand-maintained copies drifted from the Python reference" is
    # precisely the defect athenaeum#1354 fixed. So assert the invariant the
    # behavioural coverage rests on, rather than leaving it to inspection.
    def test_kill_switch_helper_is_identical_across_all_six_hooks(self) -> None:
        """All six copies of `__athenaeum_recall_disabled` are byte-identical.

        Also pins the helper to bash 3.2: `#!/usr/bin/env bash` on stock macOS
        resolves to `/bin/bash`, GNU bash 3.2.57, where the case-folding
        expansion `${v,,}` is a PARSE-time syntax error — it would take the
        whole script down, not just the kill switch, breaking these hooks'
        "must never block session startup" contract. Same reason the bash-4
        `mapfile` was removed in athenaeum#1104 and bash arrays were rejected
        in athenaeum#1343.
        """
        hooks = [
            SESSION_START,
            USER_PROMPT,
            PRE_COMPACT,
            PENDING_QUESTIONS,
            WIKI_INJECT,
            REBUILD_INDEX,
        ]
        bodies: dict[str, str] = {}
        for hook in hooks:
            lines = hook.read_text().splitlines()
            start = next(
                i for i, ln in enumerate(lines) if ln.startswith("__athenaeum_recall_disabled()")
            )
            end = next(i for i, ln in enumerate(lines[start:], start) if ln == "}")
            # Comments differ between copies by design; the code must not.
            bodies[hook.name] = "\n".join(
                ln for ln in lines[start : end + 1] if not ln.strip().startswith("#")
            )

        reference = bodies[PRE_COMPACT.name]
        for name, body in bodies.items():
            assert body == reference, f"{name}'s kill-switch helper has drifted from the others"

        # bash 4.0+ case folding (`${v,,}` / `${v^^}`) must not reappear.
        assert ",,}" not in reference and "^^}" not in reference
