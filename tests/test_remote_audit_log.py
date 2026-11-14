# SPDX-License-Identifier: Apache-2.0
"""Per-call audit log of remote tool calls (issue athenaeum#2603).

One class per acceptance criterion, reusing the real OAuth 2.1 resource-
server harness from ``tests/test_mcp_oauth_resource_server.py`` (the fake
in-process JWKS issuer, the live FastMCP ``Client`` over an ASGI transport,
and ``_drive``/``_build_app``, which already forward arbitrary
``create_server`` kwargs — this file passes ``audit_log_path`` through that
seam) rather than re-deriving a second JWT-minting harness. Cross-test-file
imports of a sibling suite's fixtures/helpers are an established pattern in
this package (see ``tests/test_audit_on_touch_run_summary.py`` importing
``_make_ctx`` from ``tests/test_librarian_run_phases.py``).

- :class:`TestEveryCallAppendsOneRecord` (AC1) — the required-field shape,
  through the real dispatch wrapper.
- :class:`TestNoSecretEverReachesTheFile` (AC2) — the security-critical
  assertion: a known secret fed through BOTH arguments and a tool's RETURN
  VALUE must never appear in the log file, end to end through the wrapper.
- :class:`TestLocationConfigurableModeAndConcurrency` (AC3) — the resolver,
  the ``0600`` mode, and line-atomicity under concurrent threads/processes.
- :class:`TestRefusedCallsAreRecorded` (AC4) — a scope refusal (through
  ``_guarded``) and a bad-token / non-allowlisted-identity refusal (through
  ``_IdentityAllowlistVerifier.verify_token``, upstream of any tool name).
- :class:`TestStdioIsUnaffectedByDefault` (AC5) — no ``serve_auth`` means no
  scope guard at all, so nothing is ever written.
"""

from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path
from typing import Any

import pytest

from athenaeum.audit_log import (
    append_audit_record,
    canonical_argument_digest,
    resolve_audit_log_file,
)
from athenaeum.config import resolve_serve_audit_log
from tests.test_mcp_oauth_resource_server import (
    _INITIALIZE,
    ALLOWED_SUBJECT,
    DEFAULT_READ_SCOPE,
    DEFAULT_WRITE_SCOPE,
    MCP_PATH,
    READ_TOOL,
    STRANGER_SUBJECT,
    WRITE_TOOL,
    _drive,
    _FakeIssuer,
    _mcp_client,
    _no_ambient_serve_auth_env,  # noqa: F401 - pytest autouse fixture
    _raw_client,
    _serve_auth,
)

#: `issuer`/`corpus` are defined LOCALLY rather than imported from
#: `tests/test_mcp_oauth_resource_server.py`, matching this package's
#: established convention (see `tests/test_mcp_remote_mode_safety.py`'s own
#: `corpus` fixture) of each OAuth-harness test file defining its own copy
#: rather than importing a sibling's `@pytest.fixture`-decorated function —
#: importing one and then using the same name as a test method PARAMETER
#: (required for pytest's fixture-by-name resolution) reads to `ruff` as
#: redefining an unused import at every single call site, which a plain
#: helper-function import (the cross-file pattern
#: `tests/test_audit_on_touch_run_summary.py` uses) does not trigger.


@pytest.fixture(scope="module")
def issuer() -> _FakeIssuer:
    """The fake issuer. Module-scoped: RSA generation is the only slow step."""
    pytest.importorskip("fastmcp")
    pytest.importorskip("httpx2")
    jwk = pytest.importorskip("joserfc.jwk")

    from athenaeum.serve_auth import SIGNING_ALGORITHM

    signer = jwk.RSAKey.generate_key(2048, private=True)
    other_signer = jwk.RSAKey.generate_key(2048, private=True)
    published = dict(signer.as_dict(private=False))
    published.update(kid="fake-issuer-1", use="sig", alg=SIGNING_ALGORITHM)
    return _FakeIssuer(
        signer=signer,
        other_signer=other_signer,
        jwks={"keys": [published]},
    )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A knowledge directory with one open page and one confidential page.

    Mirrors `tests/test_mcp_oauth_resource_server.py`'s own `corpus` fixture
    exactly (same two pages) so AC5's audience-pin non-vacuousness holds
    here too.
    """
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (knowledge / "raw").mkdir()
    (wiki / "open-widget.md").write_text(
        "---\nname: Open Widget\naccess: open\n"
        "description: A widget anybody may read\n---\n\n"
        "The open widget record.\n",
        encoding="utf-8",
    )
    (wiki / "secret-widget.md").write_text(
        "---\nname: Secret Widget\naccess: confidential\n"
        "description: A widget only the owner may read\n---\n\n"
        "The confidential widget record.\n",
        encoding="utf-8",
    )
    (knowledge / "athenaeum.yaml").write_text("search_backend: keyword\n", encoding="utf-8")
    return knowledge

#: A marker that is NOT shaped like a real secret (no PEM banner, nothing
#: named ``*_key``, nothing base64/JWT-shaped) — just a value distinctive
#: enough that its accidental presence in the audit file is unambiguous.
PLANTED_SECRET = "canary-leak-marker-f00dbeef-must-never-appear-in-audit"


def _post_initialize_with_audit(
    corpus: Path, issuer: _FakeIssuer, serve_auth: Any, token: str | None, audit_log_path: Path
) -> tuple[int, str]:
    """Like ``_post_initialize``, but forwards ``audit_log_path`` through
    ``create_server`` — needed because the AC4 bad-token/identity cases are
    refused before any session exists, so a tool call can't reach them."""

    async def _body(app: Any) -> tuple[int, str]:
        async with _raw_client(app) as client:
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            response = await client.post(MCP_PATH, headers=headers, json=_INITIALIZE)
            return response.status_code, response.headers.get("www-authenticate", "")

    return _drive(corpus, issuer, serve_auth, _body, audit_log_path=audit_log_path)


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _call_tool(
    corpus: Path,
    issuer: _FakeIssuer,
    serve_auth: Any,
    audit_log_path: Path,
    token: str,
    tool: str,
    arguments: dict,
) -> tuple[str | None, str | None]:
    """Call *tool* through a live MCP client; return (text, error)."""

    async def _body(app: Any) -> tuple[str | None, str | None]:
        async with _mcp_client(app, token) as client:
            try:
                result = await client.call_tool(tool, arguments)
            except Exception as exc:  # noqa: BLE001 - the refusal IS the result
                return None, str(exc)
            return result.content[0].text, None

    return _drive(corpus, issuer, serve_auth, _body, audit_log_path=audit_log_path)


# ---------------------------------------------------------------------------
# AC1 — every tool call appends one record with the required fields.
# ---------------------------------------------------------------------------


class TestEveryCallAppendsOneRecord:
    def test_a_successful_read_call_is_recorded_with_identity_and_digest(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        audit_log_path = tmp_path / "audit.jsonl"
        token = issuer.mint(scope=DEFAULT_READ_SCOPE, azp="client-abc", jti="jti-001")
        text, error = _call_tool(
            corpus, issuer, _serve_auth(), audit_log_path, token, READ_TOOL, {"query": "widget"}
        )
        assert error is None, error
        records = _read_records(audit_log_path)
        assert len(records) == 1
        record = records[0]
        assert record["tool"] == READ_TOOL
        assert record["outcome"] == "ok"
        assert record["sub"] == ALLOWED_SUBJECT
        assert record["client_id"] == "client-abc"
        assert record["jti"] == "jti-001"
        # FastMCP fills in every parameter's resolved default before calling
        # the wrapped function, so the hashed kwargs are not literally
        # `{"query": "widget"}` -- assert the digest is well-formed and
        # DETERMINISTIC (a second identical call hashes the same) rather
        # than predicting FastMCP's own default-filling exactly.
        assert len(record["args_sha256"]) == 64
        int(record["args_sha256"], 16)  # well-formed hex
        assert "ts" in record and record["ts"].endswith("Z")
        assert isinstance(record["result_count"], int)
        assert isinstance(record["page_uids"], list)

    def test_a_write_call_is_recorded_as_a_separate_line(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        audit_log_path = tmp_path / "audit.jsonl"
        token = issuer.mint(scope=f"{DEFAULT_READ_SCOPE} {DEFAULT_WRITE_SCOPE}")
        _call_tool(
            corpus,
            issuer,
            _serve_auth(),
            audit_log_path,
            token,
            WRITE_TOOL,
            {"content": "A fact about gadgets."},
        )
        _call_tool(
            corpus, issuer, _serve_auth(), audit_log_path, token, READ_TOOL, {"query": "widget"}
        )
        records = _read_records(audit_log_path)
        assert [r["tool"] for r in records] == [WRITE_TOOL, READ_TOOL]
        assert all(r["outcome"] == "ok" for r in records)


# ---------------------------------------------------------------------------
# AC2 — the security-critical assertion.
# ---------------------------------------------------------------------------


class TestNoSecretEverReachesTheFile:
    def test_the_pure_digest_function_is_deterministic_and_never_the_secret(self) -> None:
        """Unit-level sanity for the digest primitive itself, before the
        end-to-end assertions below drive it through the real wrapper."""
        digest = canonical_argument_digest((), {"content": PLANTED_SECRET})
        assert len(digest) == 64
        assert PLANTED_SECRET not in digest
        assert digest == canonical_argument_digest((), {"content": PLANTED_SECRET})
        assert digest != canonical_argument_digest((), {"content": PLANTED_SECRET + "x"})

    def test_a_secret_fed_through_arguments_never_appears(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        audit_log_path = tmp_path / "audit.jsonl"
        token = issuer.mint(scope=f"{DEFAULT_READ_SCOPE} {DEFAULT_WRITE_SCOPE}")
        text, error = _call_tool(
            corpus,
            issuer,
            _serve_auth(),
            audit_log_path,
            token,
            WRITE_TOOL,
            {"content": PLANTED_SECRET},
        )
        assert error is None, error
        raw = audit_log_path.read_text(encoding="utf-8")
        assert PLANTED_SECRET not in raw
        assert token not in raw
        records = _read_records(audit_log_path)
        assert len(records) == 1
        digest = records[0]["args_sha256"]
        assert len(digest) == 64
        int(digest, 16)  # well-formed hex

        # The digest is a real function of the arguments (not a constant) --
        # a second call with DIFFERENT content hashes differently. FastMCP's
        # own default-filling means this cannot be predicted against a
        # hand-built `canonical_argument_digest` call from the test side (see
        # the sibling assertion above), so sensitivity is what is checked
        # instead of an exact value.
        _call_tool(
            corpus,
            issuer,
            _serve_auth(),
            audit_log_path,
            token,
            WRITE_TOOL,
            {"content": PLANTED_SECRET + "-different"},
        )
        second_digest = _read_records(audit_log_path)[1]["args_sha256"]
        assert second_digest != digest

    def test_a_secret_fed_through_a_returned_page_never_appears(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The secret goes out in the page a remote caller recalls — the
        exact exfiltration shape this issue exists to leave a forensic trail
        for — and still must never be written to the log."""
        (corpus / "wiki" / "secret-bearing.md").write_text(
            "---\nname: Secret Bearing Widget\naccess: open\n"
            "description: carries a planted secret\n---\n\n"
            f"Widget page containing {PLANTED_SECRET}.\n",
            encoding="utf-8",
        )
        audit_log_path = tmp_path / "audit.jsonl"
        token = issuer.mint(scope=DEFAULT_READ_SCOPE)
        text, error = _call_tool(
            corpus, issuer, _serve_auth(), audit_log_path, token, READ_TOOL, {"query": "widget"}
        )
        assert error is None, error
        assert text is not None and PLANTED_SECRET in text, "sanity: the secret really came back"
        raw = audit_log_path.read_text(encoding="utf-8")
        assert PLANTED_SECRET not in raw
        assert token not in raw

    def test_an_error_outcome_never_echoes_the_exception_message(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """A tool body that raises with the planted secret embedded in its
        own exception message must still not leak it — ``_guarded`` records
        only the exception's TYPE NAME, never ``str(exc)``."""
        audit_log_path = tmp_path / "audit.jsonl"
        token = issuer.mint(scope=f"{DEFAULT_READ_SCOPE} {DEFAULT_WRITE_SCOPE}")
        # An empty `source` makes `remember_write` raise with a message that
        # echoes its own argument shape — close enough to "the exception text
        # names something caller-controlled" to exercise the property without
        # needing a bespoke failing tool.
        text, error = _call_tool(
            corpus,
            issuer,
            _serve_auth(),
            audit_log_path,
            token,
            WRITE_TOOL,
            {"content": PLANTED_SECRET, "source": ""},
        )
        raw = audit_log_path.read_text(encoding="utf-8") if audit_log_path.exists() else ""
        assert PLANTED_SECRET not in raw


# ---------------------------------------------------------------------------
# AC3 — configurable location, 0600 mode, line-atomic under concurrency.
# ---------------------------------------------------------------------------


class TestLocationConfigurableModeAndConcurrency:
    def test_resolver_precedence_cli_over_env_over_yaml_over_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "cache"
        assert resolve_audit_log_file(resolve_serve_audit_log(None), cache_dir) == (
            cache_dir / "remote-audit.jsonl"
        )
        monkeypatch.setenv("ATHENAEUM_SERVE_AUDIT_LOG", str(tmp_path / "env.jsonl"))
        assert resolve_serve_audit_log(None) == tmp_path / "env.jsonl"
        assert resolve_serve_audit_log({"serve": {"audit_log": str(tmp_path / "yaml.jsonl")}}) == (
            tmp_path / "env.jsonl"
        ), "env must win over yaml"
        assert resolve_serve_audit_log(
            {"serve": {"audit_log": str(tmp_path / "yaml.jsonl")}},
            str(tmp_path / "cli.jsonl"),
        ) == tmp_path / "cli.jsonl", "cli must win over env"
        monkeypatch.delenv("ATHENAEUM_SERVE_AUDIT_LOG", raising=False)
        assert resolve_serve_audit_log(
            {"serve": {"audit_log": str(tmp_path / "yaml.jsonl")}}
        ) == tmp_path / "yaml.jsonl"

    def test_file_is_created_at_mode_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        append_audit_record(path, {"ts": "x"})
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_concurrent_thread_appends_are_never_interleaved(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        n_threads, per_thread = 16, 25

        def _worker(worker_id: int) -> None:
            for i in range(per_thread):
                append_audit_record(
                    path, {"ts": "x", "worker": worker_id, "i": i, "pad": "y" * 200}
                )

        threads = [threading.Thread(target=_worker, args=(w,)) for w in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == n_threads * per_thread
        parsed = [json.loads(line) for line in lines]  # raises if any line is torn/merged
        seen = {(r["worker"], r["i"]) for r in parsed}
        assert len(seen) == n_threads * per_thread, "a record was lost or duplicated"

    @staticmethod
    def _process_worker(path_str: str, worker_id: int, count: int) -> None:
        path = Path(path_str)
        for i in range(count):
            append_audit_record(path, {"ts": "x", "worker": worker_id, "i": i, "pad": "z" * 200})

    def test_concurrent_process_appends_are_never_interleaved(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        n_procs, per_proc = 6, 20
        ctx = multiprocessing.get_context("fork")
        procs = [
            ctx.Process(
                target=self._process_worker, args=(str(path), w, per_proc)
            )
            for w in range(n_procs)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        assert all(p.exitcode == 0 for p in procs)

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == n_procs * per_proc
        parsed = [json.loads(line) for line in lines]
        seen = {(r["worker"], r["i"]) for r in parsed}
        assert len(seen) == n_procs * per_proc, "a record was lost or duplicated across processes"


# ---------------------------------------------------------------------------
# AC4 — refused calls (scope, bad token, non-allowlisted identity).
# ---------------------------------------------------------------------------


class TestRefusedCallsAreRecorded:
    def test_a_scope_refusal_is_recorded_through_the_dispatch_wrapper(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        audit_log_path = tmp_path / "audit.jsonl"
        token = issuer.mint(scope=DEFAULT_READ_SCOPE)
        text, error = _call_tool(
            corpus,
            issuer,
            _serve_auth(),
            audit_log_path,
            token,
            WRITE_TOOL,
            {"content": "x"},
        )
        assert text is None and error is not None
        records = _read_records(audit_log_path)
        assert len(records) == 1
        assert records[0]["tool"] == WRITE_TOOL
        assert records[0]["outcome"] == "refused"
        assert records[0]["sub"] == ALLOWED_SUBJECT

    def test_a_bad_signature_is_recorded_with_no_tool_name(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        audit_log_path = tmp_path / "audit.jsonl"
        status, _ = _post_initialize_with_audit(
            corpus,
            issuer,
            _serve_auth(),
            issuer.mint(signer=issuer.other_signer),
            audit_log_path,
        )
        assert status == 401
        records = _read_records(audit_log_path)
        assert len(records) == 1
        assert records[0]["tool"] is None
        assert records[0]["outcome"] == "refused"
        assert records[0]["reason"] == "invalid_token"
        assert records[0]["sub"] is None

    def test_a_non_allowlisted_identity_is_recorded_with_its_subject(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        audit_log_path = tmp_path / "audit.jsonl"
        status, _ = _post_initialize_with_audit(
            corpus,
            issuer,
            _serve_auth(),
            issuer.mint(sub=STRANGER_SUBJECT),
            audit_log_path,
        )
        assert status == 401
        records = _read_records(audit_log_path)
        assert len(records) == 1
        assert records[0]["tool"] is None
        assert records[0]["outcome"] == "refused"
        assert records[0]["reason"] == "identity_not_allowlisted"
        assert records[0]["sub"] == STRANGER_SUBJECT

    def test_a_request_with_no_authorization_header_writes_nothing(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Documented boundary, not a gap: FastMCP's ``RequireAuthMiddleware``
        answers a request with NO ``Authorization`` header at all (RFC 6750
        §3.1's "missing auth" case) directly at the ASGI layer -- it never
        calls ``verify_token``, so there are no claims, no token and nothing
        for ``_IdentityAllowlistVerifier`` to attribute a line to. This is
        exactly the "a TLS failure, a path typo, a scanner" class
        ``docs/guides/remote.md`` already assigns to the reverse proxy's own
        access log, not to this feature. A PRESENTED-but-invalid token (bad
        signature, wrong issuer, expired, wrong audience) DOES reach
        ``verify_token`` and IS recorded -- see the bad-signature case above.
        """
        audit_log_path = tmp_path / "audit.jsonl"
        status, _ = _post_initialize_with_audit(
            corpus, issuer, _serve_auth(), None, audit_log_path
        )
        assert status == 401
        assert not audit_log_path.exists()


# ---------------------------------------------------------------------------
# AC5 — stdio (no serve_auth) is unaffected by default.
# ---------------------------------------------------------------------------


class TestStdioIsUnaffectedByDefault:
    def test_no_serve_auth_means_no_scope_guard_and_no_file(
        self, tmp_path: Path, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """``serve_auth=None`` is what every stdio server (and every
        unauthenticated HTTP install) passes — see ``_cmd_serve.cmd_serve``'s
        own stdio branch, which nulls ``serve_auth`` before this process's
        ``audit_log_path`` is even resolved. With no ``serve_auth``,
        ``create_server`` never builds a scope guard at all (``_scope_guarded``
        is only ever called when it is set), so a tool is registered BARE —
        there is nothing left to append through, even though *audit_log_path*
        is passed here exactly as a real stdio invocation would never bother
        to.
        """
        audit_log_path = tmp_path / "audit.jsonl"

        async def _body(app: Any) -> tuple[str | None, str | None]:
            async with _mcp_client(app, token=None) as client:
                try:
                    result = await client.call_tool(READ_TOOL, {"query": "widget"})
                except Exception as exc:  # noqa: BLE001
                    return None, str(exc)
                return result.content[0].text, None

        text, error = _drive(corpus, issuer, None, _body, audit_log_path=audit_log_path)
        assert error is None, error
        assert text is not None
        assert not audit_log_path.exists(), "a file was written with no serve_auth configured"
