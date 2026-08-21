"""Shared test fixtures for athenaeum test suite."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

# Issue athenaeum#554 (L11) — re-export RecordingClient so the record/replay eval
# double is importable from a single, discoverable location alongside the
# canned-response unit double below (``from tests.conftest import
# RecordingClient``). RecordingClient wraps a REAL anthropic client and
# persists live responses to fixtures for tests/evals/harness.py's
# record/replay flow; it is NOT a canned-response fake, so it is a distinct
# tool from FakeLLMClient (which never touches the network). Imported here,
# rather than reimplemented, so there is exactly one implementation.
from tests.evals.harness import RecordingClient  # noqa: F401


def init_git_repo(root: Path, *, branch: str = "develop") -> None:
    """Initialize *root* as a git repo with an initial commit (issue athenaeum#947).

    Shared by the ``fold-into-existing`` pending-merge test fixtures —
    ``resolve_merge`` now refuses to fold outside a git repo (removal must
    stay recoverable via ``git revert``/``git show``), so every such
    fixture needs this. Centralized here rather than copy-pasted a
    seventh+ time across ``test_merge_fold_write_paths.py`` /
    ``test_pending_merges_resolve.py`` / ``test_retraction_cascade.py`` /
    ``test_merges_propose_fold_cli.py`` / ``test_merge_proposal_gates.py`` /
    ``test_t2_merge_wiring.py`` / ``test_merge_write_kind_validation.py``
    (pre-existing standalone copies in ``test_auto_memory_prune.py`` /
    ``test_corrections.py`` predate this issue and are left as-is — not
    this change's concern).

    Call AFTER writing whatever fixture files (target page, source pages,
    ...) should exist BEFORE the operation under test runs, so they land
    in the seed commit. ``ALLOW_PROTECTED_BRANCH_COMMIT=1`` is set globally
    by ``/tmp/runtests.sh`` for this repo's container, so a ``-b develop``
    (never ``-b main``, per this workspace's git-fixture convention) is not
    strictly required for the commit to succeed — kept anyway to match the
    convention used elsewhere in this suite.
    """
    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True, check=True
        )

    root.mkdir(parents=True, exist_ok=True)
    run("init", "-q", "-b", branch)
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Athenaeum Test")
    run("add", "-A")
    run("commit", "-q", "-m", "initial: seed fixture")


def make_llm_response(text: str, usage: Any = None) -> SimpleNamespace:
    """Build an anthropic-shaped ``messages.create`` response.

    Mirrors the ``_mock_response``/``_msg`` helpers duplicated across the
    ad-hoc ``_FakeClient`` doubles: ``resp.content[0].text`` plus an
    optional ``resp.usage`` with the four token counters athenaeum's spend
    ledger reads.
    """
    return SimpleNamespace(content=[SimpleNamespace(text=text)], usage=usage)


def make_llm_usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> SimpleNamespace:
    """Build the four-counter usage object athenaeum's spend ledger reads."""
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


class FakeLLMClient:
    """Canonical anthropic-shaped test double (issue athenaeum#554, L11).

    One shared implementation of the ``client.messages.create(**params)``
    slice the athenaeum call sites use, so the ~13 ad-hoc ``_FakeClient``
    copies stop drifting from the SDK shape independently. Construct with a
    response (text or a full response object) OR a callable, and it records
    the kwargs each call received for assertions.

    Construction modes (pick one):
      * ``FakeLLMClient(text="...")`` — every ``create`` call returns a
        canned-text response (``.content[0].text``).
      * ``FakeLLMClient(response=some_namespace)`` — every ``create`` call
        returns the given pre-built response object verbatim (use
        :func:`make_llm_response` to build one with ``usage`` attached).
      * ``FakeLLMClient(responder=callable)`` — ``create`` calls
        ``responder(**kwargs)``; return a response object, or a bare
        string (auto-wrapped via :func:`make_llm_response`).
      * ``FakeLLMClient(raises=SomeException("..."))`` — every ``create``
        call raises the given exception instance instead of returning.

    All modes record: ``self.calls`` (list of kwargs dicts passed to each
    ``create`` invocation, in order) and ``self.client_kwargs`` (the kwargs
    the fake was constructed with, e.g. ``api_key``/``max_retries``/
    ``timeout``, for tests asserting client-construction args like
    ``captured["__client_kwargs__"]["api_key"]`` in the pre-athenaeum#554 doubles).
    """

    def __init__(
        self,
        *,
        text: str | None = None,
        response: Any = None,
        responder: Callable[..., Any] | None = None,
        raises: BaseException | None = None,
        **client_kwargs: Any,
    ) -> None:
        self._text = text
        self._response = response
        self._responder = responder
        self._raises = raises
        self.client_kwargs = client_kwargs
        self.calls: list[dict[str, Any]] = []
        self.messages = self

    # Matches ``anthropic.Anthropic(**kwargs)`` being monkeypatched directly
    # onto an INSTANCE — call sites do
    # ``monkeypatch.setattr(anthropic, "Anthropic", fake_instance)`` and then
    # the code under test constructs via ``anthropic.Anthropic(**kwargs)``.
    # Recording ``client_kwargs`` on ``self`` (rather than returning a fresh
    # instance) means the ONE instance the test holds a reference to
    # accumulates both the construction kwargs and every ``create`` call, so
    # `fake.client_kwargs` / `fake.calls` are visible from the test without
    # threading a second handle through the monkeypatch.
    def __call__(self, **client_kwargs: Any) -> "FakeLLMClient":
        self.client_kwargs = client_kwargs
        return self

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        if self._responder is not None:
            result = self._responder(**kwargs)
            if isinstance(result, str):
                return make_llm_response(result)
            return result
        if self._response is not None:
            return self._response
        return make_llm_response(self._text or "")


@pytest.fixture
def fake_llm_client() -> Callable[..., FakeLLMClient]:
    """Factory fixture returning :class:`FakeLLMClient` for repointing tests."""
    return FakeLLMClient


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure git identity is available for tests that run git commit."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test Runner")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test Runner")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


def pytest_configure(config: pytest.Config) -> None:
    """Redirect the cache dir / spend ledger for the WHOLE suite, BEFORE collection
    (athenaeum#791; supersedes the ``_isolate_cache_dir_suite`` fixture from
    athenaeum#776).

    The fixture this replaces was session-scoped and autouse, which sounds like
    the same guarantee as "in force from the first import to the last teardown"
    — but it is not. Fixtures (of any scope) only wrap test *execution*; they
    run during the setup phase of the first test that needs them, which is
    already AFTER collection. A module that resolves the cache dir at
    *collection* time — a module-level statement, or ``pytest.mark.parametrize``
    decorator arguments, which pytest evaluates while IMPORTING the module —
    runs before any fixture has had a chance to fire, session-scoped or not.
    That gap is exactly how ``tests/test_thinking_seam.py`` needed its own
    bespoke module-level ``os.environ[...]`` workaround (deleted in this same
    change) to protect its collection-time ``@pytest.mark.parametrize(...,
    _all_params())`` call.

    ``pytest_configure`` runs once, after conftest.py discovery but BEFORE
    test collection begins — so setting the env vars here closes the gap
    structurally for every test module, instead of requiring each escaping
    module to invent its own workaround (three independent ledgers have
    already needed exactly that fix once: athenaeum#750, athenaeum#776, and
    the ``test_thinking_seam.py`` workaround this change removes).

    ``ATHENAEUM_SPEND_LEDGER`` is set explicitly alongside the cache dir
    rather than left to fall out of it, so a config carrying
    ``spend.ledger_path`` cannot route around the redirect either. Plain
    ``os.environ`` assignment (not ``monkeypatch``, which is a fixture and
    unavailable here) — undone by :func:`pytest_unconfigure` below.
    """
    cache_dir = Path(tempfile.mkdtemp(prefix="athenaeum-suite-cache-"))
    config._athenaeum_suite_cache_dir = cache_dir  # type: ignore[attr-defined]
    os.environ["ATHENAEUM_CACHE_DIR"] = str(cache_dir)
    os.environ["ATHENAEUM_SPEND_LEDGER"] = str(cache_dir / "spend.jsonl")


def pytest_unconfigure(config: pytest.Config) -> None:
    """Clean up the whole-suite cache dir :func:`pytest_configure` created."""
    cache_dir = getattr(config, "_athenaeum_suite_cache_dir", None)
    if cache_dir is not None:
        shutil.rmtree(cache_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``resolve_cache_dir(cache_dir=None)`` at a per-test tmp dir (athenaeum#750).

    Without this, any test that drives a parse site through
    ``athenaeum.llm_schemas.observe``/``record_observation`` — or anything else
    that resolves the cache dir with no explicit ``cache_dir`` argument — falls
    through ``resolve_cache_dir``'s ``arg > ATHENAEUM_CACHE_DIR env > default``
    order to the real ``~/.cache/athenaeum``, appending to the operator's
    production ``_llm_schema_observations.jsonl`` ledger on every test run.
    ``ATHENAEUM_CACHE_DIR`` here is what ``resolve_cache_dir`` and, in turn,
    ``athenaeum.llm_schemas.observations_path()`` consult, so pointing it at
    ``tmp_path`` (function-scoped, unique per test — the right granularity
    since ``tmp_path`` itself is function-scoped) redirects every no-arg
    resolution during the test into a throwaway directory.

    Belt-and-braces: also default ``ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED=0``
    under test, so a test that never touches the cache dir at all still can't
    write a ledger record by accident. Tests that specifically exercise the
    ledger (``tests/test_llm_schemas.py``) opt back in explicitly via
    ``monkeypatch.setenv("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", "1")`` and/or
    pass an explicit ``cache_dir=tmp_path`` (which wins over the env var per
    ``resolve_cache_dir``'s precedence).

    Returns the tmp cache dir in case a test wants to assert against it
    directly.

    Narrows the whole-suite ``pytest_configure`` redirect above rather than
    replacing it: that hook is the floor no test (or collection-time import)
    can escape, this one gives each test its own directory on top.
    ``ATHENAEUM_SPEND_LEDGER`` is re-pointed into the same per-test directory
    (athenaeum#776) so the ledger follows the per-test cache dir instead of
    accumulating rows from earlier tests in the suite-wide file.
    """
    cache_dir = tmp_path / ".cache-athenaeum"
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("ATHENAEUM_SPEND_LEDGER", str(cache_dir / "spend.jsonl"))
    monkeypatch.setenv("ATHENAEUM_SCHEMA_OBSERVATIONS_ENABLED", "0")
    return cache_dir


@pytest.fixture(autouse=True)
def _reset_model_rates() -> Iterator[None]:
    """Reset the ACTIVE per-MTok rate table to the code default after every
    test (issue athenaeum#783).

    ``athenaeum.models.configure_model_rates`` installs a process-wide
    mutable global (``_ACTIVE_MODEL_RATES_USD_PER_MTOK``) so
    ``TokenUsage.estimated_cost_usd`` can pick up an ``athenaeum.yaml``
    ``pricing:`` override without threading config through every call site.
    Left unreset, a test that configures a custom/partial table (e.g. the
    AC1 override test, or a preflight test that installs a table missing
    most prefixes) would leak into the NEXT test in the same session and
    silently change its pricing — the exact cross-test global-state leak the
    whole-suite ``pytest_configure`` redirect above already had to fix once
    for the cache dir (athenaeum#776). Autouse and function-scoped so no test
    can opt out or forget to clean up.
    """
    yield
    from athenaeum.models import configure_model_rates

    configure_model_rates(None)


@pytest.fixture
def wiki_dir(tmp_path: Path) -> Path:
    """Create a minimal wiki directory with sample entity pages."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()

    # Old-format page (no uid field)
    (wiki / "feedback_keychain_auth.md").write_text(textwrap.dedent("""\
        ---
        name: Auth tokens must use system keychain
        description: Never store auth tokens as plaintext env vars.
        type: feedback
        ---

        Always use the system keychain for storing auth tokens.
    """))

    # Entity-template format page
    (wiki / "a1b2c3d4-acme-corp.md").write_text(textwrap.dedent("""\
        ---
        uid: a1b2c3d4
        type: company
        name: Acme Corp
        aliases:
          - Acme
          - Acme Corporation
        access: confidential
        tags:
          - client
          - fintech
        created: '2024-03-15'
        updated: '2024-04-06'
        ---

        # Acme Corp

        Fintech startup, Series B.
    """))

    # Another old-format page
    (wiki / "project_knowledge_architecture.md").write_text(textwrap.dedent("""\
        ---
        name: Knowledge architecture project
        description: Unified knowledge system.
        type: project
        ---

        The knowledge architecture unifies fragmented memory scopes.
    """))

    # Files that should be skipped by EntityIndex
    (wiki / "_index.md").write_text("# Index\n")
    (wiki / "MEMORY.md").write_text("# Memory Index\n")

    return wiki
