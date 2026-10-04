"""Smoke tests for the Claude Code example hooks in ``examples/claude-code/``.

These hooks are load-bearing for the sidecar experience — a regression would
silently break auto-recall for all future sessions. They're shipped to users
via copy-paste, so the CI contract is: each hook must be exit-clean against
a minimal synthetic wiki on a standard POSIX box with ``bash`` (plus ``jq``
and ``sqlite3`` for the hooks that still need them) available.

**What ``TestUserPromptRecall`` tests, after issue athenaeum#1363.** That
hook is now a 13-line launcher that execs the packaged adapter
(:mod:`athenaeum.claude_code_adapter`), so the only thing left to test
THROUGH it is the black-box contract: stdin JSON in, one line of hook-output
JSON out, a ledger row on the side, silence on every no-op path. The ~35
tests that pinned the retired shell body's internals — its FTS5 ``SELECT``,
its BM25 ordering, its awk budget pass and BWK-awk portability, its
``_pm_*`` bash helpers, its "no Python interpreter is spawned on the FTS5
path" contract, and the ``ATHENAEUM_SRC`` fake-package stub that could
intercept the shell's hand-built ``sys.path`` but cannot intercept a
packaged import — were deleted with it. Every invariant they guarded that
still exists is pinned in-process instead, chiefly by
``tests/test_context_core.py`` (budget, kill switch, dedup, tier
retirement, description round-tripping, body-scoped FTS5 matching),
``tests/test_context_overflow_1905.py`` (cap, overflow notice, budget
drops) and ``tests/test_context_push_telemetry.py`` (ledger rows). The
three behaviours that did NOT survive the cutover are pinned as strict
xfails in ``TestRetiredShellParityGaps`` below rather than deleted.

``_require("jq")`` / ``_require("sqlite3")`` are no longer called in
``TestUserPromptRecall``: neither the launcher nor
``session-start-recall.sh`` shells out to either binary any more, and
leaving the guards in place would have SKIPPED the whole class on a runner
without them — a green-because-skipped result for the one thing that
changed.

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
    estimate_tokens,
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


def _env_without_op(base_env: dict[str, str]) -> dict[str, str]:
    """*base_env* with every PATH entry that provides an `op` binary removed.

    athenaeum#1886's "fetch fails or is skipped" cases need `op` provably
    absent — filtering PATH here (rather than trusting the runner's ambient
    state) keeps those tests hermetic even on a machine that happens to have
    the real 1Password CLI installed.
    """
    env = dict(base_env)
    dirs = [d for d in env.get("PATH", "").split(os.pathsep) if d]
    safe_dirs = [d for d in dirs if not os.path.isfile(os.path.join(d, "op"))]
    env["PATH"] = os.pathsep.join(safe_dirs)
    return env


def _write_fake_op(tmp_path: Path, fetched_key: str) -> Path:
    """A stub `op` binary that ignores its arguments and echoes *fetched_key*.

    Used to exercise the "fresh `op read` succeeds" path without touching a
    real 1Password session — `fetched_key` is always an obvious synthetic
    sentinel, never a real-looking credential.
    """
    bin_dir = tmp_path / "fake-op-bin"
    bin_dir.mkdir(exist_ok=True)
    op_stub = bin_dir / "op"
    op_stub.write_text(f"#!/usr/bin/env bash\necho {shlex.quote(fetched_key)}\n")
    op_stub.chmod(0o755)
    return bin_dir


def _write_shadow_athenaeum_package(tmp_path: Path) -> Path:
    """athenaeum#1826: a directory holding a SHADOW ``athenaeum`` package
    whose ``__init__.py`` unconditionally raises ``ImportError`` -- put
    first on ``PYTHONPATH``, this makes a plain ``import athenaeum`` fail
    deterministically on every runner, regardless of whether a real
    ``athenaeum`` happens to be installed in site-packages there (as CI's
    ``pip install -e .`` does). A regular package (has ``__init__.py``)
    earlier on ``sys.path`` wins immediately over anything found later --
    the same precedence rule that makes the fix under test (inserting
    ``$ATHENAEUM_SRC/src`` at the FRONT of ``sys.path`` before importing)
    the thing that lets the real ``athenaeum.search`` resolve instead of
    this shadow, rather than the shadow being skippable simply because it
    comes second."""
    shadow_root = tmp_path / "shadow-athenaeum-pythonpath"
    package_dir = shadow_root / "athenaeum"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text('raise ImportError("shadowed for test")\n')
    return shadow_root


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
        """FTS5 is built unconditionally regardless of ``search_backend``.

        The ``hook_env`` fixture's ``athenaeum.yaml`` pins ``search_backend:
        fts5`` explicitly (an fts5 opt-out, issue athenaeum#1825), so
        ``config.env`` reflects that explicit choice here -- see
        ``test_defaults_to_vector_when_no_config_key`` below for the actual
        no-yaml-key default."""
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

    def test_builds_fts5_index_when_python_cannot_import_athenaeum_directly(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#1826 counter-example (AC1): an interpreter that cannot
        `import athenaeum` on its own, with `ATHENAEUM_SRC` set, must still
        build the index via the `ATHENAEUM_SRC/src` sys.path fast path.

        Forces the precondition deterministically -- rather than relying on
        the runner's ambient install state, which CI's `pip install -e .`
        makes untrue there (a skip-on-violated-precondition version of this
        test never actually ran on CI, exactly the runner where the
        original regression slipped through) -- by putting a SHADOW
        `athenaeum` package first on `PYTHONPATH`: a package whose
        `__init__.py` unconditionally raises `ImportError`. A plain `import
        athenaeum` hits that shadow before it ever reaches any real
        installed copy, on every runner. The fix under test inserts
        `$ATHENAEUM_SRC/src` at the FRONT of `sys.path` before importing, so
        the real `athenaeum` package there is found first and the shadow is
        never reached -- proving the fast path, not merely proving nothing
        else was on sys.path. Before the fix, `session-start-recall.sh`'s
        two `athenaeum_search_only` `spec_from_file_location` loaders
        (`build_fts5_index`, `STOPWORDS`) registered `search.py` under a
        synthetic module name outside the `athenaeum` package, so
        `search.py`'s own module-level `from athenaeum.authority import
        is_pointer_stub` raised `ModuleNotFoundError` even with
        `ATHENAEUM_SRC` set -- see this class's
        `test_fts5_build_failure_is_nonzero_exit_with_stderr_not_stdout` for
        that failure mode pinned directly.
        """
        _require("bash")

        shadow_env = dict(hook_env)
        shadow_env["PYTHONPATH"] = str(_write_shadow_athenaeum_package(tmp_path))

        precondition = subprocess.run(
            [shadow_env["ATHENAEUM_PYTHON"], "-c", "import athenaeum"],
            env=shadow_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert precondition.returncode != 0, (
            "test setup bug: the shadow athenaeum package on PYTHONPATH did "
            "not block a plain `import athenaeum`"
        )

        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=shadow_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        index_db = tmp_path / ".cache" / "athenaeum" / "wiki-index.db"
        assert index_db.is_file()

    def test_defaults_to_vector_when_no_config_key(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """Issue athenaeum#1825: ``vector`` is the shipped default -- a
        knowledge root whose ``athenaeum.yaml`` names no ``search_backend``
        key at all must resolve to it, matching
        ``athenaeum.config._DEFAULTS``. Overrides the ``hook_env`` fixture's
        own explicit ``search_backend: fts5`` pin (see
        ``test_builds_fts5_index`` above) so this test exercises the real
        no-key default, not that fixture's opt-out."""
        knowledge_yaml = Path(hook_env["KNOWLEDGE_ROOT"]) / "athenaeum.yaml"
        knowledge_yaml.write_text("auto_recall: true\n")

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
        body = config_env.read_text()
        assert "SEARCH_BACKEND=vector" in body

    def test_fts5_build_failure_is_nonzero_exit_with_stderr_not_stdout(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#1826 AC1: a genuinely failed index build (no working
        `ATHENAEUM_SRC` fast path and no importable `athenaeum`) must exit
        non-zero with the traceback on stderr -- not exit 0 with the
        traceback swallowed onto stdout via the old `2>&1 || true`.

        Forces the failure deterministically the same way the counter-
        example test above forces its success: a shadow `athenaeum` package
        (its `__init__.py` unconditionally raises `ImportError`) goes first
        on `PYTHONPATH`, so a plain `import athenaeum` fails on every
        runner regardless of ambient install state. `ATHENAEUM_SRC` also
        points at a directory that provably contains no `athenaeum`, so the
        fast path contributes nothing and resolution falls through to the
        shadow -- both routes fail, which is the genuine-failure case this
        test needs.
        """
        _require("bash")

        broken_env = dict(hook_env)
        broken_env["PYTHONPATH"] = str(_write_shadow_athenaeum_package(tmp_path))
        # A src/ directory that provably does not contain athenaeum, so
        # BOTH the ATHENAEUM_SRC fast path and a plain `import athenaeum`
        # fail -- this is the genuine-failure case, distinct from the
        # fast-path-success counter-example above.
        broken_env["ATHENAEUM_SRC"] = str(tmp_path / "no-such-checkout")

        precondition = subprocess.run(
            [broken_env["ATHENAEUM_PYTHON"], "-c", "import athenaeum"],
            env=broken_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert precondition.returncode != 0, (
            "test setup bug: the shadow athenaeum package on PYTHONPATH did "
            "not block a plain `import athenaeum`"
        )

        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=broken_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, (
            "a failed FTS5 index build must exit the hook non-zero, not "
            "silently succeed"
        )
        assert "ModuleNotFoundError" in result.stderr or "Traceback" in result.stderr, (
            f"expected the import failure on stderr, got: {result.stderr!r}"
        )
        assert "Traceback" not in result.stdout, (
            f"the traceback must not land on stdout: {result.stdout!r}"
        )

        index_db = tmp_path / ".cache" / "athenaeum" / "wiki-index.db"
        assert not index_db.is_file()

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

    def test_cached_key_preserved_when_op_fetch_fails(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#1886 AC1: a cached ANTHROPIC_API_KEY line must survive
        a run where the fetch fails (here, `op` is absent from PATH
        entirely) — the config.env writers both truncate the file, and only
        the restore this issue adds carries the line forward."""
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")

        cache_dir = tmp_path / ".cache" / "athenaeum"
        cache_dir.mkdir(parents=True)
        (cache_dir / "config.env").write_text(
            "AUTO_RECALL=true\n"
            "SEARCH_BACKEND=fts5\n"
            "ANTHROPIC_API_KEY=synthetic-cached-key-preserved\n"
        )

        env = _env_without_op(hook_env)
        assert shutil.which("op", path=env["PATH"]) is None, (
            "test setup bug: `op` still reachable on the filtered PATH"
        )

        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        body = (cache_dir / "config.env").read_text()
        key_lines = [line for line in body.splitlines() if line.startswith("ANTHROPIC_API_KEY=")]
        assert key_lines == ["ANTHROPIC_API_KEY=synthetic-cached-key-preserved"]
        assert "No ANTHROPIC_API_KEY cached" not in result.stderr

    def test_fresh_op_fetch_replaces_cached_key(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#1886 AC2: a fresh successful `op read` must still
        replace the cached value, not just preserve it."""
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")

        cache_dir = tmp_path / ".cache" / "athenaeum"
        cache_dir.mkdir(parents=True)
        (cache_dir / "config.env").write_text(
            "AUTO_RECALL=true\n"
            "SEARCH_BACKEND=fts5\n"
            "ANTHROPIC_API_KEY=synthetic-cached-key-stale\n"
        )

        env = _env_without_op(hook_env)
        fake_op_dir = _write_fake_op(tmp_path, "synthetic-fresh-key-from-op")
        env["PATH"] = f"{fake_op_dir}{os.pathsep}{env['PATH']}"
        assert shutil.which("op", path=env["PATH"]) == str(fake_op_dir / "op")

        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        body = (cache_dir / "config.env").read_text()
        key_lines = [line for line in body.splitlines() if line.startswith("ANTHROPIC_API_KEY=")]
        assert key_lines == ["ANTHROPIC_API_KEY=synthetic-fresh-key-from-op"]
        assert "No ANTHROPIC_API_KEY cached" not in result.stderr

    def test_no_cached_key_and_op_failing_warns_once_on_stderr(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """athenaeum#1886 AC3: a run that ends with no key at all (nothing
        cached, and the fetch fails because `op` is absent) must print
        exactly one clear warning to stderr, so the degrade is visible
        instead of silent."""
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")

        env = _env_without_op(hook_env)
        assert shutil.which("op", path=env["PATH"]) is None, (
            "test setup bug: `op` still reachable on the filtered PATH"
        )

        result = subprocess.run(
            ["bash", str(SESSION_START)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        cache_dir = tmp_path / ".cache" / "athenaeum"
        body = (cache_dir / "config.env").read_text()
        assert "ANTHROPIC_API_KEY=" not in body
        assert result.stderr.count("No ANTHROPIC_API_KEY cached") == 1, (
            f"expected exactly one warning line, got stderr: {result.stderr!r}"
        )


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

    def test_silent_on_short_prompt(self, hook_env: dict[str, str]) -> None:
        _require("bash")
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
        _require_hook_python(hook_env, "athenaeum.search")

        _seed_realistic_tier_mix(Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki")
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, TIER_MIX_PROMPT)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout, "expected hookSpecificOutput JSON on stdout"

        # Issue athenaeum#1783: the fixed `head -3` this test was written
        # against is gone — the relevance-bounded cap's ceiling (7 by
        # default) now has room for the two `principle` pages too, and
        # they ARE genuinely relevant (their `aliases` list also contains
        # "sonderling"), so their presence alongside the person pages no
        # longer signals the old tier-backfill bug on its own. What still
        # WOULD signal that bug: a `principle` page pushed INSTEAD OF a
        # `person` page — so the assertion narrows from set-equality to
        # "every true BM25 top-3 `person` page is present", which the old
        # hot-tier gate could never satisfy (it excluded person pages
        # entirely in 10 of 12 sampled queries).
        assert set(TIER_MIX_PERSON_NAMES) <= _pushed_names(result), (
            "the hook must push the true BM25 top-3 (warm `person` pages) "
            "— the hot `principle` substitutes the gate used to backfill "
            f"with must never REPLACE them. context={result.stdout!r}"
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
        # Issue athenaeum#1783: subset, not set-equality -- see
        # `test_gate_removal_returns_the_true_bm25_top3`'s own comment for
        # why (the wider ceiling legitimately surfaces the two `principle`
        # pages alongside the person pages now). What this assertion still
        # catches: an over-applied exclusion silently dropping an ordinary
        # `person` page, which is the failure mode this test exists for.
        assert set(TIER_MIX_PERSON_NAMES) <= _pushed_names(result), (
            "ordinary pages must still be reachable, so this test can fail "
            f"if an exclusion is over-applied. context={context!r}"
        )


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

    def _set_stub_hits(
        self, fake_pkg: Path, hits: list[tuple[str, str, float]]
    ) -> None:
        """Fake `search.py`: `query_vector_index` returns fixed *hits*.

        Same stubbing technique
        `test_vector_backend_surfaces_every_page_and_records_no_tier` uses
        (deterministic, no real chromadb/embedder needed to prove the
        SHELL-SIDE floor filter works) -- widened to carry more than one hit
        so a test can assert one is kept and one is dropped by score alone.
        """
        rows = ", ".join(
            f"({fname!r}, {name!r}, {score!r})" for fname, name, score in hits
        )
        (fake_pkg / "search.py").write_text(
            "def query_vector_index(query, cache_dir, n=3, exclude=None):\n"
            "    exclude = exclude or set()\n"
            f"    hits = [{rows}]\n"
            "    return [h for h in hits if h[0] not in exclude][:n]\n"
        )

    def _vector_env(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> tuple[dict[str, str], Path]:
        """Seed the index, flip to the vector backend, and return a fake-src env.

        Shared setup lifted from
        `test_vector_backend_surfaces_every_page_and_records_no_tier` so the
        floor tests below don't re-derive it three times.
        """
        # Build the FTS5 index with the checkout's `src` on PYTHONPATH, the
        # same convention `test_topics_trace_survives_bwk_awk_semantics`
        # (elsewhere in this file) already established for this exact
        # reason: `hook_env` isolates HOME, which hides per-user
        # site-packages (PEP 370), so on a box where `athenaeum` is only
        # importable via the user site, `session-start-recall.sh`'s own
        # dev-path index build fails open and leaves NO `wiki-index.db` at
        # all -- silently, since that build is wrapped in `|| true`. Every
        # floor test below that asserts a REAL FTS5 hit is present or
        # absent would then pass VACUOUSLY (an absent hit reads the same
        # whether the floor dropped it or the index never existed to
        # produce it in the first place) -- exactly the failure mode the
        # MUST-1 positive-control test in this class exists to catch, so
        # this helper must not reintroduce it via a missing index.
        seed_env = dict(hook_env)
        real_src = str(Path(hook_env["ATHENAEUM_SRC"]) / "src")
        existing_seed_pythonpath = seed_env.get("PYTHONPATH", "")
        seed_env["PYTHONPATH"] = (
            f"{real_src}{os.pathsep}{existing_seed_pythonpath}"
            if existing_seed_pythonpath
            else real_src
        )
        self._seed_index(seed_env)
        db_file = Path(hook_env["ATHENAEUM_CACHE_DIR"]) / "wiki-index.db"
        assert db_file.exists(), (
            "the FTS5 index did not build -- every floor test using this "
            "helper would pass vacuously without it"
        )

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
        # Issue athenaeum#1665: the hook's relevance-floor mechanism
        # (`athenaeum.config`) is a PLAIN package import, deliberately not
        # routed through the `ATHENAEUM_SRC` override above. `ATHENAEUM_SRC`
        # is not test-only -- the LIVE hook sets it every turn too
        # (`session-start-recall.sh` caches it from the deploy checkout),
        # and it lets `query_vector_index` load from one known file by path
        # without a full package install. `athenaeum.config` has a much
        # wider transitive import surface than a single dev-path-loaded
        # file provides for, so it goes through normal Python package
        # resolution instead -- the same thing a real `pip install
        # athenaeum` deployment already satisfies for free. This dev
        # checkout is not installed as a package at all (see
        # `_require_hook_python`'s own docstring for the parallel PEP 370
        # isolation problem), so the checkout's real `src/` has to be put
        # on `PYTHONPATH` here to stand in for that install -- the same
        # convention this file already uses elsewhere to seed the FTS5
        # index against the checkout's own code.
        existing_pythonpath = vector_env.get("PYTHONPATH", "")
        vector_env["PYTHONPATH"] = (
            f"{real_src}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else real_src
        )
        return vector_env, fake_pkg

    def test_relevance_floor_config_writer_is_a_noop_with_no_floors(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        """AC (issue athenaeum#1761): the acceptance criterion for the
        writer itself -- with both floors ``None`` (no ``--relevance-floor-*``
        flag given), it writes nothing at all, so a default north-star
        dispatch stays byte-identical to today's behaviour.
        """
        from tests.evals.north_star_cli import write_relevance_floor_config

        knowledge = tmp_path / "unwritten-knowledge-root"
        write_relevance_floor_config(
            knowledge, relevance_floor_vector=None, relevance_floor_fts5=None, search_backend="fts5"
        )
        assert not knowledge.exists()

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
        # Issue athenaeum#1789: FTS5 now indexes page BODY, not just
        # frontmatter, so this query's "customer" term also legitimately
        # matches `lean-startup.md` (body: "...rapid iteration and
        # customer feedback") alongside `customer-development.md` -- both
        # are genuinely relevant, so the exact count is no longer pinned
        # at 1. The byte-shape/telemetry contract this test exists to pin
        # is checked over every item, not just a single hardcoded one.
        assert isinstance(rec["items"], list) and len(rec["items"]) >= 1
        assert rec["pushed_count"] == len(rec["items"])
        assert isinstance(rec["token_cost"], int)
        assert rec["token_cost_estimated"] is True
        assert rec["source"] == "sidecar"

        items_by_id = {it["id"]: it for it in rec["items"]}
        assert "customer-development.md" in items_by_id
        for item in rec["items"]:
            assert isinstance(item["id"], str) and item["id"]
            assert item["tier"] == "internal"
            assert isinstance(item["scope"], str)
            assert isinstance(item["token_cost"], int)
            # `relevance` and a per-item `backend` are ABSENT, and that is
            # the canonical shape rather than a loss: the retired shell hook
            # hand-wrote both as extra keys, but
            # `athenaeum.push_metrics.PushedItem` has only ever carried
            # id/tier/scope/token_cost, and the backend is a record-level
            # field (asserted above). Issue athenaeum#1363.
            assert "relevance" not in item
            assert "backend" not in item
            # Issue athenaeum#1514 retired the retrieval-cost vocabulary, so a
            # hook-written row carries NO `memory_tier` key at all. Absence is
            # the contract, not an empty string: `athenaeum.push_metrics` reads
            # an absent key as "written after the retirement", which an empty
            # value could not be distinguished from. The fixture page still
            # carries the orphaned frontmatter key -- nothing reads it, and
            # nothing rewrote the corpus to remove it (that is this issue's
            # stated frontmatter migration).
            assert "memory_tier" not in item, (
                "the retired tier vocabulary must not reappear in telemetry: "
                f"{item}"
            )
        pushed_page = (
            Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki" / "customer-development.md"
        )
        assert "memory_tier: hot" in pushed_page.read_text()

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
        _require_hook_python(hook_env, "athenaeum.search")

        _seed_realistic_tier_mix(Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki")
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, TIER_MIX_PROMPT)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # Issue athenaeum#1783: subset, not set-equality -- see
        # `test_gate_removal_returns_the_true_bm25_top3`'s own comment.
        assert set(TIER_MIX_PERSON_NAMES) <= _pushed_names(result)

        records = read_push_records(
            wiki_root=Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki",
            cache_dir=Path(hook_env["ATHENAEUM_CACHE_DIR"]),
        )
        items = [it for rec in records for it in rec["items"]]
        # Issue athenaeum#1783: at LEAST the three person pages -- the
        # wider ceiling can legitimately also push the two `principle`
        # pages now (same reasoning as the `_pushed_names` assertion
        # above), so this is no longer an exact count.
        assert len(items) >= 3, f"expected at least the three person pages: {items}"
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
            hits=[("some-mcp-page.md", {}, estimate_tokens("a rendered snippet of text"))],
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

    def test_scope_from_audience_public_marker_only_resolves_open(
        self, hook_env: dict[str, str]
    ) -> None:
        """Defect 2 (functional): the exact audience shape that used to
        crash under bash 3.2 -- `|__access_open__|`, a public page with
        NO roles -- must resolve to `scope: "open"` end to end, proving
        the array-free rewrite is still correct, not just array-free.
        """
        _require("bash")
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
        # Matched as a whole line rather than with a trailing "\n": the
        # retired shell hook appended a newline after EVERY bullet including
        # the last (a known defect of that hook -- see
        # tests/evals/test_adapter_overflow_breadcrumb_1905.py); the packaged
        # renderer joins bullets instead. Issue athenaeum#1363.
        assert "  - Nodescriptiontest Page" in context.splitlines()
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
        # Take the bullet as a LINE: the last bullet has no trailing newline
        # to index to any more (see the dangling-separator test above for why).
        bullet = next(ln for ln in context.splitlines() if marker in ln)
        rendered_desc = bullet[bullet.index(marker) + len(marker) :]
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
        # Record-level, not per-item: see the item-shape pin below.
        assert records[0]["backend"] == "fts5"

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
            # Exactly `athenaeum.push_metrics.PushedItem`'s four fields. The
            # retired shell hook also wrote `relevance` and a per-item
            # `backend`; the packaged writer never did, and since the hook
            # delegates to it (issue athenaeum#1363) the sidecar row and the
            # MCP row now have one shape between them, not two.
            assert set(item) == {
                "id",
                "tier",
                "scope",
                "token_cost",
            }, f"push record item shape changed: {sorted(item)}"
            assert "topics" not in item

        raw_line = durable_push_records_path(wiki_root, cache_dir=cache_dir).read_text()
        assert "topics" not in raw_line

    def test_fts5_query_does_not_match_body_only_term(
        self, hook_env: dict[str, str]
    ) -> None:
        """Issue athenaeum#1789 (Quine follow-up): schema v6 added a
        ``body`` column to the ``wiki`` FTS5 table
        (``athenaeum.search.FTS5Backend``), which this hook's own raw FTS5
        query never asked for -- before this fix, ``WHERE wiki MATCH
        '${FTS_QUERY}'`` (no column filter) started matching body content
        too, diluting this query's relevance the same way the Python-side
        hybrid fusion's FTS5 arm was diluted (see
        ``FTS5Backend.query``'s ``metadata_only`` parameter). Pins that the
        hook's column-filter prefix (``{filename name tags aliases
        description}: (...)``) restores the pre-v6 behavior: a page whose
        ONLY matching term lives in its body must not surface through this
        hook, exactly as it would not have before the FTS5 body column
        existed.
        """
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")

        wiki = Path(hook_env["KNOWLEDGE_ROOT"]) / "wiki"
        (wiki / "body-only-term.md").write_text(
            "---\n"
            "name: Unrelated Title\n"
            "tags: [misc]\n"
            "description: Nothing about the query here\n"
            "---\n\n"
            "This body mentions zzzquokkabodyterm nowhere else on the page.\n"
        )
        self._seed_index(hook_env)

        result = self._run_hook(hook_env, "tell me about zzzquokkabodyterm")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        context = (
            json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            if result.stdout.strip()
            else ""
        )
        assert "Unrelated Title" not in context, (
            "a body-only term must not surface the page through the hook's "
            f"FTS5 query (schema v6 body dilution regression): got {context!r}"
        )


class TestRetiredShellParityGaps:
    """Three things the retired shell hook did that the packaged adapter does
    NOT do yet (issue athenaeum#1363).

    Issue athenaeum#1361 cut the live ``UserPromptSubmit`` hook over from
    ``examples/claude-code/user-prompt-recall.sh`` to
    :mod:`athenaeum.claude_code_adapter`, and issue athenaeum#1661 audited
    that cutover for drift and closed six points. Retiring the shell body
    (this issue) removed the ~1650 lines that were the only remaining
    implementation of three MORE, and this class is the record of them. They
    are deltas against the retired shell, **already live since the cutover**
    -- none of them is introduced by the de-forking commit, and none is fixed
    by it either, because fixing any of them changes what the live recall
    path pushes into every turn and belongs in its own change with its own
    eval receipt (``.github/workflows/eval-receipt-check.yml``), not in a
    commit whose job is deleting dead code.

    Each gap gets a PAIR of tests, deliberately:

    * a plain test that the knob still RESOLVES, so an ``xfail`` below can
      never be an artifact of a mis-built fixture -- the single failure mode
      that would otherwise turn this record into a decoration; and
    * a ``strict=True`` ``xfail`` test of the behaviour itself, so whoever
      closes the gap is told by a FAILING suite to come back and delete the
      marker rather than leaving a stale "known broken" note behind.

    Do not "fix" one of these by relaxing the assertion.
    """

    def _seed(self, hook_env: dict[str, str]) -> None:
        subprocess.run(
            ["bash", str(SESSION_START)],
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

    def _run(self, hook_env: dict[str, str], prompt: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(USER_PROMPT)],
            input=json.dumps({"prompt": prompt, "session_id": f"test-{uuid.uuid4().hex}"}),
            env=hook_env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _bullets(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        if not result.stdout.strip():
            return []
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        return [ln for ln in context.splitlines() if ln.startswith("  - ")]

    # -- gap 1: the push-token budget ---------------------------------------

    def test_push_token_budget_still_resolves(self) -> None:
        """Control for the xfail below: the knob itself is intact."""
        from athenaeum.config import resolve_push_token_budget

        assert resolve_push_token_budget({"push_budget": {"tokens_per_turn": 1}}) == 1

    def test_core_still_enforces_a_budget_when_given_one(self) -> None:
        """Second control: the enforcement exists in the core, pinned by
        ``tests/test_context_core.py::
        test_budget_skips_a_candidate_that_would_exceed_it``. So gap 1 is
        purely a missing argument at the adapter's call site, not missing
        machinery -- the same shape as the ``n`` ceiling issue athenaeum#1661
        found and closed.
        """
        import inspect

        from athenaeum.context import build_context_for_turn

        assert "budget" in inspect.signature(build_context_for_turn).parameters

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "athenaeum#1363 gap 1: the adapter never passes `budget=` to "
            "build_context_for_turn, so ATHENAEUM_PUSH_TOKEN_BUDGET and "
            "push_budget.tokens_per_turn are both inert on the per-turn "
            "sidecar path. The retired shell hook enforced them in its awk "
            "budget pass. Delete this marker when the adapter forwards the "
            "resolved budget."
        ),
    )
    def test_push_token_budget_is_honoured_on_the_sidecar_path(
        self, hook_env: dict[str, str]
    ) -> None:
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed(hook_env)

        generous = dict(hook_env, ATHENAEUM_PUSH_TOKEN_BUDGET="10000")
        assert self._bullets(
            self._run(generous, "tell me about customer development frameworks")
        ), "precondition: a generous budget must push at least one bullet"

        stingy = dict(hook_env, ATHENAEUM_PUSH_TOKEN_BUDGET="1")
        assert not self._bullets(
            self._run(stingy, "tell me about customer development frameworks")
        ), "a 1-token budget must not be able to afford any bullet"

    # -- gap 2: the recall relevance floor ----------------------------------

    def test_relevance_floor_still_resolves(self) -> None:
        """Control for the xfail below."""
        from athenaeum.config import resolve_recall_relevance_floor

        assert (
            resolve_recall_relevance_floor(
                {"recall": {"relevance_floor": {"push": {"fts5": -1.0}}}},
                "fts5",
                unprompted=True,
            )
            == -1.0
        )

    def test_relevance_floor_is_enforced_on_the_mcp_path(self) -> None:
        """Second control: the floor IS wired -- just not on this path. It is
        called from :mod:`athenaeum.mcp_server` (the explicit ``recall``
        tool) and nowhere in :mod:`athenaeum.context`, which is what the
        sidecar runs.
        """
        mcp = (Path(__file__).parent.parent / "src" / "athenaeum" / "mcp_server.py").read_text()
        ctx = (Path(__file__).parent.parent / "src" / "athenaeum" / "context.py").read_text()
        assert "meets_relevance_floor" in mcp
        assert "meets_relevance_floor" not in ctx

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "athenaeum#1363 gap 2: athenaeum.context never calls "
            "meets_relevance_floor, so recall.relevance_floor.{fts5,vector} "
            "(and the ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5 env override) are "
            "inert on the per-turn sidecar path. The retired shell hook "
            "imported both resolvers and filtered its own rows with them. "
            "Delete this marker when context.py enforces the floor."
        ),
    )
    def test_relevance_floor_is_honoured_on_the_sidecar_path(
        self, hook_env: dict[str, str]
    ) -> None:
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed(hook_env)

        assert self._bullets(
            self._run(hook_env, "tell me about customer development frameworks")
        ), "precondition: the probe query must push at least one bullet unfiltered"

        # FTS5 `rank` is negative-is-better, so a floor of -1e9 admits
        # everything and +1e9 admits nothing: the direction is
        # `athenaeum.search.meets_relevance_floor`'s, not this test's guess.
        floored = dict(hook_env, ATHENAEUM_RECALL_PUSH_MIN_SCORE_FTS5="1000000000")
        assert not self._bullets(
            self._run(floored, "tell me about customer development frameworks")
        ), "every hit is below an impossibly strict floor and must be dropped"

    # -- gap 3: the local topics trace --------------------------------------

    def test_the_topics_trace_reader_still_exists(self) -> None:
        """Control for the xfail below: ``athenaeum viewer`` still reads the
        trace, so the file going unwritten leaves that column permanently
        empty rather than removing a surface nobody consults.
        """
        from athenaeum._cmd_viewer import _load_topics_for_query_hash

        assert callable(_load_topics_for_query_hash)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "athenaeum#1363 gap 3: `_last_turn_topics.jsonl` (issue "
            "athenaeum#1530) was written only by the retired shell hook's "
            "`_pm_write_topics_trace`. Nothing in src/ writes it, so "
            "`athenaeum viewer`'s topics column is empty for every turn "
            "since the athenaeum#1361 cutover. Delete this marker when the "
            "adapter (or the core) writes the trace again."
        ),
    )
    def test_the_topics_trace_is_written_on_the_sidecar_path(
        self, hook_env: dict[str, str]
    ) -> None:
        _require("bash")
        _require_hook_python(hook_env, "athenaeum.search")
        self._seed(hook_env)

        result = self._run(hook_env, "tell me about customer development frameworks")
        assert self._bullets(result), "precondition: the turn must push something"

        trace = Path(hook_env["ATHENAEUM_CACHE_DIR"]) / "_last_turn_topics.jsonl"
        assert trace.exists(), f"no topics trace at {trace}"


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
    def test_kill_switch_helper_is_identical_across_every_hook_that_has_one(
        self,
    ) -> None:
        """Every shell copy of `__athenaeum_recall_disabled` is byte-identical.

        `user-prompt-recall.sh` is deliberately NOT in this list any more
        (issue athenaeum#1363): it is a thin launcher for the packaged
        adapter, and its kill switch is the one the adapter already honours
        inside `athenaeum.context` (`_recall_disabled`, itself pinned against
        `athenaeum.killswitch` by `tests/test_context_core.py::
        test_kill_switch_short_circuits_to_empty_envelope`). The black-box
        proof that the launcher still no-ops when disabled is
        `test_user_prompt_recall_noops_when_disabled` below -- so dropping it
        here removes a source-text comparison, not a guarantee.

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
            PRE_COMPACT,
            PENDING_QUESTIONS,
            WIKI_INJECT,
            REBUILD_INDEX,
        ]
        assert "__athenaeum_recall_disabled" not in USER_PROMPT.read_text(), (
            "the thin launcher must not grow its own copy of the kill-switch "
            "helper; the adapter it delegates to already honours it"
        )
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
