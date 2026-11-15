# SPDX-License-Identifier: Apache-2.0
"""``serve.auth.allowed_clients`` and its per-client read-only cap (issue athenaeum#2602).

The identity allowlist decides WHO may use the server. When the authorization
server lets clients register dynamically, any client the operator signs in
with gets a token the server accepts. A client allowlist pins WHICH apps may
use it as well. A ``read_only: true`` entry takes the write scope away from
one app, so that app cannot save memories even when its token carries write.

Covered, against a live in-process server built by ``create_server``:

- unset list: unchanged behaviour (a token naming no client is accepted);
- a listed client is accepted, by ``azp`` or by ``client_id``;
- an unlisted client, a token naming no client, and a token whose two client
  claims disagree are each refused with a 401;
- the refusal is logged with the client id and never the token;
- a read-only client cannot ``remember`` and nothing is written, can still
  ``recall``, and does not see the write tools in ``tools/list``;
- the cap never grants a scope the token lacks;
- configuration validation and the docs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from athenaeum.cli import main
from athenaeum.config import resolve_serve_auth_allowed_clients
from athenaeum.serve_auth import ServeAuthError, build_serve_auth
from tests.test_mcp_oauth_resource_server import (
    DEFAULT_READ_SCOPE,
    DEFAULT_WRITE_SCOPE,
    ISSUER,
    READ_TOOL,
    RESOURCE,
    SIGNING_KID,
    WRITE_TOOL,
    _Capture,
    _drive,
    _FakeIssuer,
    _mcp_client,
    _no_ambient_serve_auth_env,  # noqa: F401 - pytest autouse fixture
    _post_initialize,
    _serve_auth,
)

#: A Client ID Metadata Document id -- a URL, which is the shape that makes
#: pinning practical. Case matters in its path, so it is mixed case on purpose.
LISTED_CLIENT = "https://app.example.invalid/OAuth/client.json"
READ_ONLY_CLIENT = "https://browser.example.invalid/client.json"
UNLISTED_CLIENT = "https://rogue.example.invalid/client.json"

BOTH_SCOPES = f"{DEFAULT_READ_SCOPE} {DEFAULT_WRITE_SCOPE}"


# `issuer` / `corpus` are local copies, per the convention
# `tests/test_remote_audit_log.py` explains.
@pytest.fixture(scope="module")
def issuer() -> _FakeIssuer:
    pytest.importorskip("fastmcp")
    pytest.importorskip("httpx2")
    jwk = pytest.importorskip("joserfc.jwk")

    from athenaeum.serve_auth import SIGNING_ALGORITHM

    signer = jwk.RSAKey.generate_key(2048, private=True)
    other_signer = jwk.RSAKey.generate_key(2048, private=True)
    published = dict(signer.as_dict(private=False))
    published.update(kid=SIGNING_KID, use="sig", alg=SIGNING_ALGORITHM)
    return _FakeIssuer(signer=signer, other_signer=other_signer, jwks={"keys": [published]})


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
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
    (knowledge / "athenaeum.yaml").write_text("search_backend: keyword\n", encoding="utf-8")
    return knowledge


def _pinned(**overrides: Any) -> Any:
    clients: list[object] = [LISTED_CLIENT, {"id": READ_ONLY_CLIENT, "read_only": True}]
    return _serve_auth(allowed_clients=clients, **overrides)


def _status(corpus: Path, issuer: _FakeIssuer, serve_auth: Any, token: str) -> int:
    return _post_initialize(corpus, issuer, serve_auth, token)[0]


def _call(
    corpus: Path, issuer: _FakeIssuer, serve_auth: Any, token: str, tool: str, arguments: dict
) -> tuple[str | None, str | None]:
    async def _body(app: Any) -> tuple[str | None, str | None]:
        async with _mcp_client(app, token) as client:
            try:
                result = await client.call_tool(tool, arguments)
            except Exception as exc:  # noqa: BLE001 - the refusal IS the result
                return None, str(exc)
            return result.content[0].text, None

    return _drive(corpus, issuer, serve_auth, _body)


def _tool_names(corpus: Path, issuer: _FakeIssuer, serve_auth: Any, token: str) -> list[str]:
    async def _body(app: Any) -> list[str]:
        async with _mcp_client(app, token) as client:
            return sorted(tool.name for tool in await client.list_tools())

    return _drive(corpus, issuer, serve_auth, _body)


class TestTheAllowlistAdmitsOnlyListedClients:
    def test_an_unset_list_is_a_no_op(self, corpus: Path, issuer: _FakeIssuer) -> None:
        """No list: the client is not consulted, so a token naming none passes."""
        auth = _serve_auth()
        assert auth.allowed_clients is None
        assert _status(corpus, issuer, auth, issuer.mint()) == 200
        assert _status(corpus, issuer, auth, issuer.mint(azp=UNLISTED_CLIENT)) == 200

    @pytest.mark.parametrize("claim", ["azp", "client_id"])
    def test_a_listed_client_is_accepted(
        self, corpus: Path, issuer: _FakeIssuer, claim: str
    ) -> None:
        assert _status(corpus, issuer, _pinned(), issuer.mint(**{claim: LISTED_CLIENT})) == 200

    def test_both_claims_naming_the_same_listed_client_is_accepted(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        token = issuer.mint(azp=LISTED_CLIENT, client_id=LISTED_CLIENT)
        assert _status(corpus, issuer, _pinned(), token) == 200

    @pytest.mark.parametrize("claim", ["azp", "client_id"])
    def test_an_unlisted_client_is_refused(
        self, corpus: Path, issuer: _FakeIssuer, claim: str
    ) -> None:
        assert _status(corpus, issuer, _pinned(), issuer.mint(**{claim: UNLISTED_CLIENT})) == 401

    def test_a_token_naming_no_client_is_refused(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Fail closed: under a configured list, no claim is not a pass."""
        assert _status(corpus, issuer, _pinned(), issuer.mint()) == 401
        assert _status(corpus, issuer, _pinned(), issuer.mint(azp="  ")) == 401

    def test_disagreeing_client_claims_are_refused(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """A listed ``azp`` cannot vouch for an unlisted ``client_id``."""
        token = issuer.mint(azp=LISTED_CLIENT, client_id=UNLISTED_CLIENT)
        assert _status(corpus, issuer, _pinned(), token) == 401

    def test_matching_is_exact_not_case_folded(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        token = issuer.mint(azp=LISTED_CLIENT.lower())
        assert _status(corpus, issuer, _pinned(), token) == 401

    def test_an_identity_refusal_still_comes_first(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """A listed client does not rescue an identity that is not allowlisted."""
        token = issuer.mint(sub="auth0|stranger-9", azp=LISTED_CLIENT)
        assert _status(corpus, issuer, _pinned(), token) == 401


class TestTheRefusalIsLoggedWithTheClientIdNeverTheToken:
    def test_log_names_the_client_and_not_the_token(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        capture = _Capture()
        capture.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        target = logging.getLogger("athenaeum.mcp_auth")
        level, propagate = target.level, target.propagate
        target.setLevel(logging.DEBUG)
        target.addHandler(capture)
        token = issuer.mint(azp=UNLISTED_CLIENT)
        try:
            assert _status(corpus, issuer, _pinned(), token) == 401
        finally:
            target.removeHandler(capture)
            target.setLevel(level)
            target.propagate = propagate
        refusals = [line for line in capture.lines if "allowed_clients" in line]
        assert refusals, "the client refusal was not logged"
        assert any(UNLISTED_CLIENT in line for line in refusals)
        haystack = "\n".join(capture.lines)
        for segment in token.split("."):
            assert segment not in haystack


class TestAReadOnlyClientCannotWrite:
    def test_a_read_only_client_is_refused_on_remember(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """Even with the write scope in its token, and nothing is written."""
        token = issuer.mint(azp=READ_ONLY_CLIENT, scope=BOTH_SCOPES)
        text, error = _call(
            corpus, issuer, _pinned(), token, WRITE_TOOL, {"content": "A fact about widgets."}
        )
        assert text is None, f"remember succeeded for a read-only client: {text!r}"
        assert error is not None
        assert "read-only" in error and WRITE_TOOL in error
        assert not list((corpus / "raw").rglob("*.md"))

    def test_a_read_only_client_can_recall(self, corpus: Path, issuer: _FakeIssuer) -> None:
        token = issuer.mint(azp=READ_ONLY_CLIENT, scope=BOTH_SCOPES)
        text, error = _call(corpus, issuer, _pinned(), token, READ_TOOL, {"query": "widget"})
        assert error is None, error
        assert text is not None and "widget" in text.lower()

    def test_a_full_access_client_on_the_same_server_can_remember(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        """The positive control: the cap is per client, not server-wide."""
        token = issuer.mint(azp=LISTED_CLIENT, scope=BOTH_SCOPES)
        text, error = _call(
            corpus, issuer, _pinned(), token, WRITE_TOOL, {"content": "A fact about widgets."}
        )
        assert error is None, error
        assert list((corpus / "raw").rglob("*.md")), "remember wrote nothing"

    def test_write_tools_are_absent_from_a_read_only_clients_tools_list(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        auth = _pinned()
        capped = _tool_names(
            corpus, issuer, auth, issuer.mint(azp=READ_ONLY_CLIENT, scope=BOTH_SCOPES)
        )
        full = _tool_names(corpus, issuer, auth, issuer.mint(azp=LISTED_CLIENT, scope=BOTH_SCOPES))
        assert READ_TOOL in capped
        assert WRITE_TOOL not in capped
        assert WRITE_TOOL in full
        assert set(capped) < set(full)

    def test_no_read_only_entry_leaves_tools_list_unchanged(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        listed_only = _serve_auth(allowed_clients=[LISTED_CLIENT])
        token = issuer.mint(azp=LISTED_CLIENT)
        assert _tool_names(corpus, issuer, listed_only, token) == _tool_names(
            corpus, issuer, _serve_auth(), issuer.mint()
        )


class TestTheCapOnlyNarrows:
    def test_capped_scopes_removes_write_and_never_adds(self) -> None:
        auth = _pinned()
        ro = {"azp": READ_ONLY_CLIENT}
        assert auth.capped_scopes(ro, [DEFAULT_READ_SCOPE, DEFAULT_WRITE_SCOPE]) == [
            DEFAULT_READ_SCOPE
        ]
        # A read-only client with no scope gets none -- the cap grants nothing.
        assert auth.capped_scopes(ro, []) == []
        assert auth.capped_scopes(ro, [DEFAULT_WRITE_SCOPE]) == []
        full = {"azp": LISTED_CLIENT}
        assert auth.capped_scopes(full, [DEFAULT_WRITE_SCOPE]) == [DEFAULT_WRITE_SCOPE]
        assert auth.capped_scopes(full, []) == []

    def test_a_read_only_client_without_the_read_scope_cannot_recall(
        self, corpus: Path, issuer: _FakeIssuer
    ) -> None:
        token = issuer.mint(azp=READ_ONLY_CLIENT, scope=DEFAULT_WRITE_SCOPE)
        text, error = _call(corpus, issuer, _pinned(), token, READ_TOOL, {"query": "widget"})
        assert text is None and error is not None


class TestConfiguration:
    def test_the_resolver_reads_yaml_only(self) -> None:
        assert resolve_serve_auth_allowed_clients(None) is None
        assert resolve_serve_auth_allowed_clients({"serve": {"auth": {}}}) is None
        entries = [LISTED_CLIENT, {"id": READ_ONLY_CLIENT, "read_only": True}]
        cfg = {"serve": {"auth": {"allowed_clients": entries}}}
        assert resolve_serve_auth_allowed_clients(cfg) == entries
        as_string = {"serve": {"auth": {"allowed_clients": "a-client, b-client"}}}
        assert resolve_serve_auth_allowed_clients(as_string) == ["a-client", "b-client"]

    def test_a_non_list_value_is_refused(self) -> None:
        with pytest.raises(ServeAuthError):
            resolve_serve_auth_allowed_clients({"serve": {"auth": {"allowed_clients": 3}}})

    def test_entries_are_parsed(self) -> None:
        auth = _pinned()
        assert auth.allowed_clients == frozenset({LISTED_CLIENT, READ_ONLY_CLIENT})
        assert auth.read_only_clients == frozenset({READ_ONLY_CLIENT})

    def test_a_client_listed_twice_takes_the_narrower_entry(self) -> None:
        auth = _serve_auth(
            allowed_clients=[{"id": LISTED_CLIENT, "read_only": True}, LISTED_CLIENT]
        )
        assert auth.read_only_clients == frozenset({LISTED_CLIENT})

    def test_an_empty_list_is_refused(self) -> None:
        with pytest.raises(ServeAuthError, match="empty"):
            _serve_auth(allowed_clients=[])

    @pytest.mark.parametrize(
        "entry",
        [
            3,
            {"id": ""},
            {"client": LISTED_CLIENT},
            {"id": LISTED_CLIENT, "read_only": "yes"},
            {"id": LISTED_CLIENT, "readonly": True},
        ],
    )
    def test_a_malformed_entry_is_refused(self, entry: object) -> None:
        with pytest.raises(ServeAuthError):
            _serve_auth(allowed_clients=[entry])

    def test_a_client_list_alone_is_a_partial_block(self) -> None:
        with pytest.raises(ServeAuthError, match="issuer"):
            build_serve_auth(allowed_clients=[LISTED_CLIENT])

    def test_serve_passes_the_list_through_and_reports_it(
        self,
        corpus: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import athenaeum.mcp_server as mcp_mod

        captured: dict[str, Any] = {}

        class _Server:
            def run(self, *_args: object, **_kwargs: object) -> None:
                raise KeyboardInterrupt

        def _fake_create_server(**kwargs: object) -> _Server:
            captured.update(kwargs)
            return _Server()

        monkeypatch.setattr(mcp_mod, "create_server", _fake_create_server)
        (corpus / "athenaeum.yaml").write_text(
            "search_backend: keyword\n"
            "serve:\n"
            "  auth:\n"
            f"    issuer: {ISSUER}\n"
            f"    resource: {RESOURCE}\n"
            "    allowed_identities: [auth0|operator-1]\n"
            "    allowed_clients:\n"
            f"      - {LISTED_CLIENT}\n"
            f"      - id: {READ_ONLY_CLIENT}\n"
            "        read_only: true\n",
            encoding="utf-8",
        )
        assert main(["serve", "--path", str(corpus), "--transport", "http"]) == 0
        built = captured["serve_auth"]
        assert built.allowed_clients == frozenset({LISTED_CLIENT, READ_ONLY_CLIENT})
        assert built.read_only_clients == frozenset({READ_ONLY_CLIENT})
        assert "[auth] client allowlist: 2 clients, 1 read-only" in capsys.readouterr().err


class TestDocs:
    ROOT = Path(__file__).resolve().parent.parent

    def test_the_setting_and_its_fail_closed_semantics_are_documented(self) -> None:
        for page in ("docs/modules/mcp.md", "docs/reference/configuration.md"):
            text = (self.ROOT / page).read_text(encoding="utf-8")
            assert "serve.auth.allowed_clients" in text, page
            assert "read_only" in text, page
        mcp = (self.ROOT / "docs/modules/mcp.md").read_text(encoding="utf-8")
        assert "fails closed" in mcp
