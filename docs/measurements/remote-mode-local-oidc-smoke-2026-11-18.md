<!-- SPDX-License-Identifier: Apache-2.0 -->

# Remote mode end-to-end smoke against a local OIDC provider, 2026-11-18

Part of athenaeum#2389 (epic athenaeum#2199). Result: **PASS on every check that a
local run can reach; a public-host run still remains** for the four things listed
under [What this run does not prove](#what-this-run-does-not-prove).

Athenaeum#2389 asks for a live run on a real host with a real identity provider. This
run substitutes a throwaway local stack for the host, the domain and the tenant, and
keeps everything else real: a real OIDC provider issuing real signed tokens, real TLS
in front of `athenaeum serve --transport http --profile remote`, and the remote
deployment guide ([Remote mode](../guides/remote.md)) followed command for command.
The in-process tests use a fake issuer; this run does not.

## Environment

| Item | Value |
| --- | --- |
| athenaeum | 0.26.3, `develop` at the tip of the run's base commit (60f851e8), installed with `uv pip install ".[mcp]"` into a fresh venv |
| Python | 3.13 (`python:3.13-slim-bookworm` container) |
| fastmcp | 4.2.1 |
| Identity provider | self-hosted OIDC provider (Keycloak 26.0, `start-dev`), one invented realm |
| TLS reverse proxy | Caddy 2.8 with its internal CA (`tls internal`), `flush_interval -1` as in the guide |
| Names | `memory.smoke.test` (athenaeum) and `id.smoke.test` (provider), both invented, resolvable only on a private container network |
| Users | two invented users in the invented realm, one on the allowlist and one not |
| Corpus | four invented wiki pages (a person, a project, a company, and one `access: confidential` page), one invented excluded-surface contact record, `athenaeum init` then `athenaeum reindex` |
| Client | a scripted OAuth 2.1 authorization-code + PKCE `S256` client plus the fastmcp MCP client, in a container on the same network, trusting the proxy's CA |
| Serve flags | exactly the guide's "equivalent as flags" block, `--audience ops`, `--auth-allowed-identities` set to the allowlisted user's verified email |

The provider was configured with: a public client with PKCE `S256` required, the
`athenaeum:read` and `athenaeum:write` scopes as default client scopes, and an audience
mapper that stamps `https://memory.smoke.test/mcp` into `aud`. The mapper stands in for
the guide's "the `resource` parameter, mapped to the token audience" requirement; whether
the provider would have done that on its own was not tested.

Teardown: all four containers, the private network and the throwaway corpus were
removed after the run.

## Results per acceptance criterion

| Check | Result | Evidence |
| --- | --- | --- |
| Unauthenticated `tools/list` gets `401` with `WWW-Authenticate` | **PASS** | `401`; header `Bearer scope="athenaeum:read athenaeum:write", resource_metadata="https://memory.smoke.test/.well-known/oauth-protected-resource/mcp"` |
| RFC 9728 metadata resolves through the proxy | **PASS** | the path-scoped URL returns the protected-resource JSON naming the resource, the provider as `authorization_servers`, and both scopes; the bare `/.well-known/oauth-protected-resource` is `404`, as the guide says |
| Full OAuth flow completes and lists tools | **PASS (scripted client, not MCP Inspector)** | discovery from the `401` challenge, to the protected-resource metadata, to the provider metadata, then authorization-code + PKCE `S256` with `resource=`, a token exchange, and `tools/list` over streamable HTTP through the proxy. The token carried the right `iss`, `aud`, `scope` and a verified email |
| Remote profile hides `resolve_question`, `resolve_merge`, `review_audit_item` | **PASS** | 14 tools served; the three are absent, and `call_tool("resolve_merge")` returns `Unknown tool`. The `[remote-policy]` startup line names the same three |
| Token for an identity not on the allowlist gets `401` | **PASS** | a valid, correctly signed, correct-audience token for the non-allowlisted user gets `401` with the same `invalid_token` challenge a bad signature gets; the audit log records `reason: identity_not_allowlisted` for the operator only |
| `recall` returns results from the invented corpus | **PASS** | three pages for `kiln calibration`; the `confidential` page never appears under `--audience ops` |
| `recall(with_pii=True)` returns no excluded value | **PASS** | the result renders the page and the "excluded fields withheld" notice; neither the invented email nor the invented phone number appears. Control: the same call against the same corpus with no remote pin returns both values, so the pin is what withholds them |
| One `remember` lands under the remote source with host-neutral provenance | **PASS** | the file landed at `raw/remote-session/<timestamp>-<id>.md` with `source: remote:inferred`, not `claude:inferred`. See finding 2 for whether it was then recallable |
| First-call latency from a cold process | **PASS, measured** | see below |
| Findings summarized on athenaeum#2199 | **not done by this lane** | left to the operator; the findings are below and in the draft issue bodies |

### MCP Inspector

MCP Inspector was not run. Its browser flow needs a browser trusting the private CA and
resolving the invented names, and its CLI mode does not drive an OAuth flow. The
scripted client performs the same steps an Inspector run would (discovery from the
challenge, PKCE `S256`, `resource`, bearer call), which covers the handshake
mechanics but not Inspector's own client behaviour.

### Cold-process latency

Three runs, each killing the server, starting a fresh `athenaeum serve` process, and
timing from the process spawn. The first-call figure includes the MCP `initialize`
round trip, JWT verification against the already-fetched key set, and one `recall`.
The key set is fetched once at startup, so the provider round trip is inside the
"ready" figure, not the first call.

| Run | spawn to TLS endpoint answering `401` | MCP `initialize` | first `recall` | second `recall` | spawn to first result |
| --- | --- | --- | --- | --- | --- |
| 1 | 0.54 s | 0.12 s | 0.12 s | under 0.01 s | 0.79 s |
| 2 | 0.61 s | 0.06 s | 0.12 s | 0.00 s | 0.80 s |
| 3 | 0.60 s | 0.07 s | 0.13 s | 0.00 s | 0.80 s |

A fresh process answers its first authenticated call in roughly **0.8 s** from spawn. The
corpus is four pages, the search backend is FTS5, the provider and proxy are one hop away
on a local network, and the OS page cache is warm. So this bounds the process-start cost,
not the latency a remote host will see on a large store over the public internet.

## Findings

Each of these is a bug or a gap against the guide, found by following it verbatim. None
blocked the run. Draft issue bodies exist for each; none has been filed.

1. **`athenaeum init` dies with a raw traceback when no `git` binary is present.** A
   slim server image has none. The prerequisites in the guide and the install page do not
   list it, and the failure is a `FileNotFoundError: 'git'` stack trace with no recovery
   hint.
2. **A remote `remember` is invisible to every remote caller under the required
   `--audience` pin.** The guide says a note written under `raw/remote-session/` "is
   recallable in the same session as the write". With `--audience ops` (which the remote
   profile requires) the note, written untagged, was indexed after `reindex` and still
   never returned to the allowlisted writer. Restarting with `--audience owner` returned
   it. The `remember` tool takes no `access` or `audience` argument, so a remote caller
   has no way to make its own write readable. The guide's claim is true only for
   `owner`.
3. **The startup `search_backend=vector but no index` warning is misleading when
   `chromadb` is not installed.** `reindex` fell back to FTS5 and `recall` worked, but
   `serve` still warned that recall would return nothing and told the operator to run the
   reindex that had already run. The guide's install route (the `mcp` extra) does not
   include the vector extra, so this is the default experience of a guide-following
   install.
4. **`remember` returns an absolute server filesystem path to the remote caller**
   (`Saved to /opt/knowledge/raw/remote-session/...`). The guide's own premise is that
   every returned value is egress to the host provider; this one discloses the server's
   layout for no benefit to the caller.
5. **The guide does not mention that `athenaeum serve` makes an outbound request to
   PyPI on startup** (the fastmcp update check, one `GET` visible in the startup log).
   For a deployment run with restricted egress it is a surprise. fastmcp documents
   `FASTMCP_CHECK_FOR_UPDATES` as the switch; this run did not test that it suppresses the
   request.

Not defects, but worth knowing:

- The guide's `curl` verification lines need `curl`, which a slim image also lacks.
- The guide's sample bind is `127.0.0.1` with the proxy on the same host. This run put the
  proxy in a separate container, so athenaeum bound `0.0.0.0` on a private container
  network, which the configured issuer permits. A same-host deployment, which is what the
  guide describes, was not reproduced.
- The `401` for a non-allowlisted identity carries the generic "your client should
  automatically re-register" hint. That is deliberate (the response must not be
  distinguishable from a bad token) but it is advice that cannot help that caller.

## What this run does not prove

- **A publicly trusted certificate chain.** The proxy used a private CA that the client
  was told to trust. Nothing here exercises a public CA, certificate renewal, OCSP, or a
  client that refuses an unknown issuer, which is what a hosted connector does.
- **A real remote client reaching it.** No ChatGPT connector, no Claude.ai connector, no
  other hosted agent connected. The client was a script on the same network, so nothing
  here proves a host's egress addresses, its timeouts, its header handling, or the way it
  reads the `WWW-Authenticate` challenge. This is athenaeum#2200's territory.
- **Dynamic client registration or client ID metadata documents against a hosted
  provider.** The scripted client used one pre-registered public client. An anonymous
  registration attempt against the local provider was refused by its default
  trusted-hosts policy and was not pursued, so DCR is untested. CIMD was not offered by
  the local provider at all.
- **A hosted provider's behaviour.** Whether a hosted tenant honours RFC 8707 `resource`
  into `aud` natively, which `iss` form it publishes, how it signs, and how it rotates
  keys. Here the audience came from an explicit mapper, and the provider was local.
- **Production-shaped latency and load.** A four-page corpus, FTS5 only, no vector index,
  one client, no network path longer than a container hop.
- **MCP Inspector itself.** See above.
- **A same-host proxy with a loopback bind**, the exact shape the guide recommends.

## Recommendation

The local run is enough to close the *mechanism* half of athenaeum#2389: discovery
through a proxy, TLS in front of the server, a real provider's tokens verified, the
allowlist, the remote tool profile, the excluded-field pin, host-neutral provenance, and
process-start latency all behave as designed. It is not enough to close the issue as
written, because its title and acceptance criteria name a real host and a real hosted
provider. Remaining for a public-host run:

1. MCP Inspector (or a real remote client) completing the OAuth flow over a publicly
   trusted certificate.
2. DCR or CIMD against the hosted provider the deployment will really use.
3. That provider's `resource` to `aud` mapping and issuer form, checked in its own
   discovery document.
4. The off-host `curl` pair from the guide run from a network that is not the host's.
