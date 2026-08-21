"""Shared test fixtures for athenaeum test suite."""

from __future__ import annotations

import errno
import ipaddress
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

# Issue athenaeum#1091 — must be set BEFORE chromadb is ever imported by any
# test module (conftest.py is imported first, at collection time, ahead of
# every test module chromadb might reach). chromadb's ``Settings`` is a
# pydantic ``BaseSettings`` that reads ``ANONYMIZED_TELEMETRY`` from the
# environment; leaving it default-True would make posthog telemetry a SECOND
# outbound-network source for the default suite, independent of the ONNX
# model download that ``_offline_embedding_function`` below neutralizes. (In
# the installed chromadb version the default ``Posthog`` telemetry client's
# ``capture()`` is already a no-op, so this is belt-and-suspenders — but the
# no-op-ness is an implementation detail of chromadb's vendored client, not
# a documented contract, so setting the flag explicitly rather than relying
# on it.) ``os.environ.setdefault`` (not ``monkeypatch``, unavailable at
# import time) so a human's real ``ANONYMIZED_TELEMETRY`` env override wins.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

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


def _default_selection(request: pytest.FixtureRequest) -> bool:
    """True unless the test is marked ``eval``, ``live``, or ``embedding``
    (issue athenaeum#1091).

    Both fixtures below only engage for the default (non-eval/live/embedding)
    pytest selection — an ``eval``/``live`` test opted into the real network,
    and an ``embedding``-marked test (e.g.
    ``test_chain_transitive_repartition``) specifically needs the REAL
    MiniLM model's cosine values, not the offline lexical stand-in, and
    needs real network egress to fetch it (see ``evals.yml``'s
    ``embedding-suite`` job) — the offline embedding stand-in / network
    guard would just break both.
    """
    node = request.node
    return not (
        node.get_closest_marker("eval")
        or node.get_closest_marker("live")
        or node.get_closest_marker("embedding")
    )


@pytest.fixture(autouse=True)
def _offline_embedding_function(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route chromadb's default embedding function through a deterministic,
    offline stand-in for the whole default (non-eval/live/embedding) suite
    (athenaeum#1091).

    ``VectorBackend``'s default-model path passes ``embedding_function=None``
    through to chromadb (``src/athenaeum/search.py:_embedding_function``), and
    the read-side calls (``query()``, and ``_get_ef``/``embed_texts`` when
    they construct a real ``DefaultEmbeddingFunction``) don't pass an
    embedding function at all. In every one of those cases chromadb's own
    fall-through (``CollectionCommon._embed``, and ``get_collection``'s
    ``embedding_function: ... = DefaultEmbeddingFunction()`` default
    parameter) resolves to ``chromadb.utils.embedding_functions
    .DefaultEmbeddingFunction``, whose ``__call__`` lazily constructs a real
    ``ONNXMiniLM_L6_V2`` and calls it — that construction+call is the thing
    that downloads the ONNX model over HTTP on first use.

    So the one seam that intercepts EVERY default-model code path — build,
    incremental add, string-query embed — without touching any
    ``src/athenaeum`` production code, is ``ONNXMiniLM_L6_V2`` itself:
    ``DefaultEmbeddingFunction.__call__`` re-imports it (``from
    chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import
    ONNXMiniLM_L6_V2``) fresh on every call, so patching the class attribute
    on its module is picked up immediately. Swapping
    ``VectorBackend._embedding_function`` instead would NOT have worked: that
    method already returns ``None`` for the default model in production, and
    ``query()``/``embed_texts()`` never call it at all.

    Never touches ``SentenceTransformerEmbeddingFunction`` (the non-default
    ``embedding_model`` branch) — no default-selection test exercises a real
    non-default model (``test_embedding_model_swap_forces_full_rebuild``
    stubs ``_embedding_function`` itself), so that branch is left alone.
    """
    if not _default_selection(request):
        return
    try:
        import chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 as onnx_module
    except ImportError:
        return  # chromadb (or its optional [vector] extra) isn't installed here

    from tests.offline_embeddings import OfflineONNXMiniLMStub

    monkeypatch.setattr(onnx_module, "ONNXMiniLM_L6_V2", OfflineONNXMiniLMStub)


@pytest.fixture(autouse=True)
def _fast_retry_backoff_in_default_suite(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neutralize ``athenaeum._retry.with_retry``'s real backoff sleep for
    the default (non-eval/live/embedding) suite (issue athenaeum#1091).

    Fixing ``NetworkBlockedInDefaultSuite``'s exception type (below) makes a
    blocked connection fail as fast, cleanly-classified ``httpx.ConnectError``
    -- but a connection error is STILL a legitimate, intentionally-retried
    condition for both the ``anthropic`` SDK's own internal retry (2 attempts)
    and athenaeum's OWN ``with_retry`` wrapper around it (5 attempts,
    exponential backoff up to a 60s cap) -- retrying is the CORRECT
    production behavior for a transient network blip, so the exception-type
    fix alone does not (and should not try to) eliminate the retries
    themselves. What it should not do is spend real wall-clock time
    sleeping between retries that are guaranteed-futile in an
    intentionally-offline test suite: several tests (e.g.
    tests/test_contradiction_sweep.py, tests/test_live_delta_cadence.py)
    exercise ``librarian.run()``'s full auto-memory phase against a REAL
    (deliberately fake-keyed, per their own ``monkeypatch.setenv
    ("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")``) anthropic client
    that, before this fix existed, relied on a real, unblocked, fast-failing
    (401, not retried) network round-trip -- a call this issue's whole point
    is to stop making by default. Once genuinely blocked, that round-trip
    becomes a real (correctly retried) connection failure, and
    ``with_retry``'s default backoff (real ``time.sleep``, exponential,
    5 attempts) turns a sub-second test into one taking 80-100+ seconds
    (measured on tests/test_contradiction_sweep.py and
    tests/test_live_delta_cadence.py individually) -- multiplied across every
    such test in the default suite, this is a real wall-clock regression.

    ``with_retry``'s ``sleep`` parameter is keyword-only and explicitly
    documented as the test seam for exactly this ("Injectable sleep, patched
    in tests so they don't wait") -- but it is a per-call default bound to
    the REAL ``time.sleep`` function object at ``athenaeum._retry`` import
    time (``sleep: Callable[[float], None] = time.sleep``), so a plain
    ``monkeypatch.setattr(time, "sleep", ...)`` does NOT reach call sites
    that don't pass ``sleep=`` explicitly (none of the production call
    sites do -- that kwarg exists for tests to use). Rewriting the ONE
    dict entry backing that default (``with_retry.__kwdefaults__["sleep"]``,
    a real, mutable dict for keyword-only defaults) reaches every call site
    uniformly, for the whole default suite, without touching any
    ``src/athenaeum`` file. Retry COUNT and classification are unaffected --
    only the sleep DURATION between retries drops to ~0, so a test that
    asserts on retry attempts/log messages (e.g. tests/test_retry.py) is
    unaffected, and any test that already passes its own ``sleep=`` spy
    (the documented, correct way to test backoff timing itself) still gets
    its own explicit value, since an explicit kwarg always overrides a
    default.

    That alone was still not enough: a faulthandler dump on the still-slow
    tests showed the same real wall-clock cost one layer DEEPER than
    athenaeum's own wrapper -- inside the ``anthropic`` SDK's OWN internal
    retry (``DEFAULT_MAX_RETRIES=2``), which sleeps via a direct,
    dynamically-looked-up ``time.sleep(timeout)`` call in
    ``SyncAPIClient._sleep_for_retry`` (``anthropic/_base_client.py``) --
    entirely independent of ``with_retry``'s injectable ``sleep`` above,
    since that call happens INSIDE ``client.messages.create(...)``, below
    where ``with_retry`` can reach. Patched narrowly (the method on the
    class, not a blanket ``time.sleep`` override) so it cannot affect any
    OTHER test's real, legitimate use of ``time.sleep`` for actual timing
    behavior elsewhere in this 6600+-test suite -- only anthropic's own
    retry-backoff wait is skipped, for both the sync and async client
    (batch/async code paths use the latter).
    """
    if not _default_selection(request):
        return
    try:
        from athenaeum._retry import with_retry
    except ImportError:
        pass
    else:
        if with_retry.__kwdefaults__ is not None and "sleep" in with_retry.__kwdefaults__:
            monkeypatch.setitem(with_retry.__kwdefaults__, "sleep", lambda _delay: None)

    try:
        import anthropic._base_client as anthropic_base_client
    except ImportError:
        return  # anthropic not installed here; nothing to patch

    def _no_sleep_for_retry(self: object, **_kwargs: object) -> None:
        return None

    async def _no_async_sleep_for_retry(self: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        anthropic_base_client.SyncAPIClient, "_sleep_for_retry", _no_sleep_for_retry
    )
    monkeypatch.setattr(
        anthropic_base_client.AsyncAPIClient,
        "_sleep_for_retry",
        _no_async_sleep_for_retry,
    )


class NetworkBlockedInDefaultSuite(ConnectionError):
    """Raised when the default (non-eval/live) pytest selection attempts an
    outbound connection to a non-local address (issue athenaeum#1091).

    Subclasses ``ConnectionError`` (an ``OSError`` subclass) DELIBERATELY,
    not ``RuntimeError``: httpcore/httpx only map ``OSError`` subclasses
    raised during ``connect()`` into ``httpx.ConnectError`` (see
    ``httpx._transports.default.map_httpcore_exceptions``). A plain
    ``RuntimeError`` escapes that mapping entirely, so a caller like the
    ``anthropic`` SDK's connection-pool/retry classifier
    (``_should_retry_exception`` in ``anthropic/_base_client.py``) never
    recognizes it as the connection failure it is — it falls through to a
    long default-timeout wait path instead of the fast
    ``ConnectionRefusedError`` failure this guard is standing in for
    (observed: ``tests/test_live_delta_cadence.py`` went from ~7s to a
    240s+ hang under the ``RuntimeError`` version, entirely inside
    ``anthropic/_base_client.py:_sleep_for_retry``). Every default-selection
    test that reaches a real, unstubbed network client (chromadb's ONNX
    download, or a test-fixture fake-keyed ``anthropic`` client — see
    ``tests/test_contradiction_sweep.py``/``tests/test_live_delta_cadence.py``,
    which deliberately exercise a real client against a REAL, normally
    fast-failing 401) needs this exception to be type-indistinguishable
    from the genuine ``ConnectionRefusedError`` this container's blocked
    egress already produces for the chromadb case, so both fail equally
    fast. Naming the destination in the message is still deliberate — this
    is meant to be immediately diagnosable from a CI failure, not just
    "test hung/failed for some reason."
    """


def _is_local_address(address: object) -> bool:
    """True for loopback/AF_UNIX destinations; False for anything routable.

    ``address`` is whatever ``socket.socket.connect``/``connect_ex`` or
    ``socket.create_connection`` received: a ``(host, port)`` tuple for
    AF_INET/AF_INET6, a path string for AF_UNIX, or (rarely) something else.
    Non-tuple addresses (AF_UNIX, or a shape we don't recognize) are treated
    as local — this guard's job is blocking outbound network egress, not
    policing IPC.
    """
    if isinstance(address, str):
        return True  # AF_UNIX path
    try:
        host = address[0]
    except (TypeError, IndexError, KeyError):
        return True
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname that isn't a loopback literal — treat as remote


@pytest.fixture(autouse=True)
def _block_non_local_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the ``pyproject.toml`` invariant: the default pytest selection
    performs no outbound network request (issue athenaeum#1091).

    Patches ``socket.socket.connect``/``connect_ex`` (covers raw sockets and
    most HTTP client libraries, including httpx's sync transport when it
    connects directly) and ``socket.create_connection`` (httpx/httpcore's
    usual path) to reject any non-local destination with a named,
    immediately-diagnosable error instead of hanging or silently succeeding
    against a real network. AF_UNIX and loopback (``127.0.0.0/8``, ``::1``,
    ``localhost``) are allowed. Disabled for tests marked ``eval``, ``live``,
    or ``embedding`` — all three opted into the real network (see
    ``_default_selection`` above).

    See ``tests/test_network_guard.py`` for the test that proves this
    fixture actually blocks (and that it allows loopback) — required so the
    pin itself is covered, not just asserted in a docstring.
    """
    if not _default_selection(request):
        return

    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create_connection = socket.create_connection

    def guarded_connect(self: socket.socket, address: Any, *a: Any, **kw: Any) -> Any:
        if not _is_local_address(address):
            raise NetworkBlockedInDefaultSuite(
                f"blocked outbound socket.connect to {address!r}: the default "
                "pytest selection must stay offline (athenaeum#1091) — mark "
                "the test `eval` or `live` if it legitimately needs the network"
            )
        return orig_connect(self, address, *a, **kw)

    def guarded_connect_ex(
        self: socket.socket, address: Any, *a: Any, **kw: Any
    ) -> Any:
        # connect_ex's documented contract is to RETURN an error code rather
        # than raise (that's the whole point of the "_ex" variant) — a
        # caller doing `if sock.connect_ex(addr): ...` without a try/except
        # would be broken by an unexpected raise here. Return the same
        # errno a real blocked/refused connection would (ECONNREFUSED)
        # instead, so this stays behaviorally consistent with connect_ex
        # everywhere else, not just for the exception TYPE reasoning
        # documented on NetworkBlockedInDefaultSuite above.
        if not _is_local_address(address):
            logging.getLogger(__name__).warning(
                "blocked outbound socket.connect_ex to %r: the default pytest "
                "selection must stay offline (athenaeum#1091) — mark the test "
                "`eval` or `live` if it legitimately needs the network",
                address,
            )
            return errno.ECONNREFUSED
        return orig_connect_ex(self, address, *a, **kw)

    def guarded_create_connection(address: Any, *a: Any, **kw: Any) -> Any:
        if not _is_local_address(address):
            raise NetworkBlockedInDefaultSuite(
                f"blocked outbound socket.create_connection to {address!r}: the "
                "default pytest selection must stay offline (athenaeum#1091) — "
                "mark the test `eval` or `live` if it legitimately needs the network"
            )
        return orig_create_connection(address, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)


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
