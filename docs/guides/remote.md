**Reference:** [mcp](../modules/mcp.md) · [sensitivity](../modules/sensitivity.md)

# Remote mode: serving athenaeum over HTTPS

> **Experimental.** `athenaeum serve --transport http` and the `serve.auth.*`
> configuration keys are experimental and are not covered by the
> `athenaeum.__all__` semver promise, so their flags, keys and formats may change
> in a minor release.

Remote mode is `athenaeum serve --transport http` behind a TLS reverse proxy, with
an external OIDC authorization server in front of it and the `remote` serve profile
pinned on. It is how a host that cannot launch a local process — ChatGPT, the Grok
apps — reaches a knowledge store at all.

It is also the configuration with the largest blast radius in this project, so read
[Every recall result is egress](#every-recall-result-is-egress) before you read the
commands.

Throughout, `memory.example.net` is the athenaeum deployment and `id.example.net` is
the authorization server. Both are invented; substitute your own.

## What athenaeum supplies, and what you supply

athenaeum ships one half of remote mode: an **OAuth 2.1 resource server**. It verifies
bearer tokens somebody else issued, pins one audience, enforces an identity allowlist
and a read/write scope split, and serves RFC 9728 protected resource metadata. See
[the MCP surface](../modules/mcp.md#authentication--a-resource-server-only) for the
full mechanism and every refusal it makes.

You supply everything else, and none of it is optional:

| Piece | Who owns it |
|---|---|
| TLS certificate and termination | you — a reverse proxy |
| The DNS name and the host it runs on | you |
| The authorization server (issuance, consent, PKCE, client registration) | you, or a hosted provider |
| Keeping the corpus compiled and indexed | you — a scheduler |
| Deciding which identities may read your memory | you — the allowlist |

athenaeum never issues a token, stores a credential, shows a consent screen or
terminates TLS. If you do not already run an authorization server, athenaeum is not
the component that gives you one.

This guide does not cover host-specific connector setup (see
[Other clients and platforms](clients.md)), and it does not cover hosting: no
provider, no image, no IaC. The deployment boundary is deliberate — see
[ChatGPT host and remote mode](../design/host-chatgpt.md) for the reasoning.

## Every recall result is egress

**Every value any tool returns leaves your machine and goes to the remote host
provider.** Not a summary of it, not an embedding of it — the page text itself.
`recall` results, `read_entity` fields, `enumerate_entities` rows, the bodies of
pending questions: all of it crosses into a third party's infrastructure, is
processed by their model, and lands in their logs and their retention policy, not
yours.

Three consequences worth stating in full:

- **Recall into a remote session is an egress event you cannot take back.** Once a
  page has been pushed to a host provider, athenaeum's own erasure cascade has no
  reach into the copies that session created — the provider's transcript, their
  logs, anything a downstream tool wrote from that session's output. This is the
  disclosed gap in [security posture](../design/security-posture.md), not a new one
  remote mode introduces; remote mode is what makes it routine.
- **The audience pin is the only thing narrowing what is reachable.**
  `athenaeum serve --audience ops` is a read filter, and the remote caller cannot
  widen it. Everything inside that filter is in scope for egress.
- **There is no per-request audience.** One process serves one audience and one
  resource. If two callers should see different subsets, run two processes.

If a class of content must never reach a third-party provider, do not put it behind
a remote server. Pin the audience so the content is not reachable, or run a separate
store. A narrower prompt is not a control.

## Prerequisites

Before you start, have all of these:

1. **athenaeum installed with the MCP extra**, on the host that will serve —
   see [Install](install.md#i-want-agents-to-write-and-read-memory-through-mcp).
   `--transport http` needs `fastmcp`, which that extra pulls in.
2. **An initialized, compiled knowledge store** on that host, with a search index
   built: `athenaeum init`, then at least one `athenaeum run` and
   `athenaeum reindex`. A remote host pointed at an unindexed store gets empty
   `recall` results and no error.
3. **A DNS name you control**, resolving to the host — `memory.example.net` below.
4. **A TLS certificate** for that name, and a reverse proxy to terminate it.
5. **An OAuth 2.1 / OIDC authorization server** meeting the requirements in the next
   section — `id.example.net` below. A hosted identity provider is the normal answer.
6. **The `sub` or verified email of every identity** that may use the server. There
   is no "anyone my provider vouches for" setting; an empty allowlist is a startup
   error.
7. **A scheduler** — systemd timers or cron. No Claude Code `SessionStart` hook runs
   on a server, so nothing compiles or reindexes unless you schedule it.

A loopback dry run first is worth the five minutes, because it separates athenaeum
configuration errors from proxy and provider errors:

```bash
athenaeum serve --path ~/knowledge --transport http --port 8000 --profile remote
```

That binds `127.0.0.1` only and needs no auth block at all. Confirm it starts and
prints its transport line, then stop it and configure the rest.

## What your authorization server must provide

A remote MCP host authenticates as a public client against **your** authorization
server. These are the properties it needs from one. Check them in the provider's own
discovery document (`https://id.example.net/.well-known/openid-configuration`)
before you deploy — every one of them fails at connector-registration time or at
first call, not at athenaeum startup.

- **PKCE with `S256`, advertised.** `code_challenge_methods_supported` must contain
  `S256`. A remote host cannot hold a client secret, so proof key for code exchange
  is the only thing binding the authorization code to the client that requested it.
  Advertised matters as much as supported: hosts read the discovery document and
  refuse to register when the method is absent.
- **Client ID metadata documents, or dynamic client registration.** A remote host
  arrives without a pre-registered client id. Either the provider accepts a client
  ID metadata document (a URL the host publishes, which the provider fetches), or it
  supports RFC 7591 dynamic client registration. A provider that only accepts
  hand-registered clients from its own console works only if the host lets you paste
  a client id, and most do not.
- **An exact issuer match.** The `iss` claim on every token must equal
  `serve.auth.issuer` byte for byte. A trailing slash, `http` where you configured
  `https`, or a tenant alias instead of the canonical issuer are all total failures,
  not partial ones.
- **The `resource` parameter, mapped to the token audience.** The host sends
  RFC 8707 `resource=https://memory.example.net/mcp` when it requests a token; the
  provider must honour it and put that value in the token's `aud` claim. A provider
  that ignores `resource` and stamps its own default audience issues tokens
  athenaeum rejects, and the rejection looks identical to a bad signature.
- **The `openid` and `email` scopes enabled.** The allowlist matches on `sub`, or on
  `email` — and only when the same token carries `email_verified: true`. Without
  those scopes the token carries nothing to match, and every caller is refused.
- **The two athenaeum scopes issuable.** Register `athenaeum:read` and
  `athenaeum:write` (or whatever you rename them to with `serve.auth.scopes`) as
  scopes the provider will grant. A token carrying neither can call nothing.
- **A JSON Web Key Set at the plain OIDC default path, signed `RS256`.** athenaeum
  reads the key set from `/.well-known/jwks.json` under the configured issuer, and
  accepts `RS256` only. Neither is configurable: there is no JWKS-URI setting and no
  algorithm knob.

That last point is the one that most often surprises an operator, so read it twice.
The derivation is right for the hosted providers that publish at the OIDC default —
Auth0, Clerk, WorkOS, Descope and Supabase among them. It is **wrong** for an issuer
that serves its key set from a tenant-scoped path, which includes a typical Okta or
Microsoft Entra issuer. Fetch `<your issuer>/.well-known/jwks.json` yourself and
confirm you get a key set back:

```bash
curl -fsS https://id.example.net/.well-known/jwks.json | head -c 200
```

A 404 there means this setting set cannot serve your provider, and the symptom in
production is every token being rejected with nothing in the logs pointing at the
key set.

## athenaeum's settings

Three blocks matter: the transport, the resource server, and the remote profile. All
three resolve **CLI flag > environment variable > `athenaeum.yaml`**.

```yaml
# ~/knowledge/athenaeum.yaml
serve:
  transport: http
  host: 127.0.0.1          # keep it loopback; the proxy is the only thing exposed
  port: 8000
  profile: remote
  excluded_fields: off
  audience: ops            # optional read scope; see below
  auth:
    issuer: https://id.example.net
    resource: https://memory.example.net/mcp
    allowed_identities:
      - ops@example.net
      - 4e6f-operator-subject-id
    scopes:
      read: athenaeum:read
      write: athenaeum:write
```

The equivalent as flags, which is what a service unit carries:

```bash
athenaeum serve --path ~/knowledge \
  --transport http --host 127.0.0.1 --port 8000 --path-prefix /mcp \
  --profile remote --excluded-fields off --audience ops \
  --auth-issuer https://id.example.net \
  --auth-resource https://memory.example.net/mcp \
  --auth-allowed-identities ops@example.net \
  --auth-scopes read=athenaeum:read,write=athenaeum:write
```

What each block buys you:

| Setting | Why it is here |
|---|---|
| `serve.transport: http` | The only transport a remote host can reach. Binds a socket; `stdio` does not. |
| `serve.host`, `serve.port` | Keep the bind on loopback and let the proxy be the only listener on a routable address. Configuring an issuer *permits* a routable bind — it does not make one a good idea. |
| `--path-prefix` | The URL path the endpoint is mounted at, default `/mcp`. CLI-only: unlike the three above it has no environment variable and no yaml key. |
| `serve.profile: remote` | Does not serve `resolve_question`, `resolve_merge` or `review_audit_item` — adjudicating your own decision queue is an owner action. `remember` and `raise_decision` stay on, because a remote agent contributing memories is the point. |
| `serve.excluded_fields: off` | Withholds every off-corpus excluded value. The `remote` profile already pins this; setting it explicitly documents the intent. |
| `serve.audience` | A read filter pinned for the life of the process. Untagged, `confidential` and `personal` pages become invisible. The caller cannot widen it. |
| `serve.auth.issuer` | Whose signatures this server trusts. Setting it is also what lifts the non-loopback bind refusal — the same act that makes every request carry a verified token. |
| `serve.auth.resource` | The one audience this process serves, and the `resource` value the host must request. |
| `serve.auth.allowed_identities` | `sub` values, or issuer-verified emails. Never empty. |
| `serve.auth.scopes` | Rename the read/write pair to scopes your provider actually issues. |

`serve.auth.issuer`, `serve.auth.resource` and `serve.auth.allowed_identities` are a
set: configure all three or none. A half-configured block exits non-zero before
anything binds rather than falling back to no auth. Under `stdio` the whole block is
inert and says so on startup.

Confirm the server came up the way you meant by reading its own startup lines
(they go to stderr):

```
[auth] OAuth 2.1 resource server: issuer https://id.example.net, audience https://memory.example.net/mcp, 1 allowed identity, scopes athenaeum:read athenaeum:write
[remote-policy] excluded fields: off (withheld); tools not served: resolve_merge, resolve_question, review_audit_item
[transport] streamable HTTP on http://127.0.0.1:8000/mcp
```

A missing `[auth]` line on an HTTP server is the whole failure: it means the block
did not resolve and the process is serving without authentication.

## The reverse proxy

The proxy terminates TLS for `memory.example.net` and forwards to the loopback
listener. athenaeum speaks plain HTTP to it and never sees a certificate.

Whatever proxy you already run is the right one. These are the properties it has to
have:

- **Terminate TLS for the exact name in `serve.auth.resource`.** The resource URL,
  the metadata the server advertises and what the host dials must be one value.
- **Forward to loopback**, not to a routable interface.
- **Pass the `Authorization` header through untouched.** A proxy that strips or
  rewrites it turns every request into a 401.
- **Do not buffer responses.** Streamable HTTP is a streaming transport; a buffering
  proxy makes long calls look like hangs.
- **Expose the well-known path.** A resource URL with a path is published at the
  path-scoped variant RFC 9728 §3.1 defines, so
  `/.well-known/oauth-protected-resource/mcp` must be reachable, not just `/mcp`.
- **Do not add a second authentication layer in front of the MCP endpoint.** An
  HTTP-basic prompt, or an IP allowlist a remote host cannot satisfy, breaks the
  OAuth flow rather than hardening it. The one exception is a client certificate,
  which the next section is about.

A minimal Caddy site block, as a shape to translate:

```caddyfile
memory.example.net {
	reverse_proxy 127.0.0.1:8000 {
		flush_interval -1
	}
}
```

`flush_interval -1` is the no-buffering knob; every proxy has one under a different
name. Verify from off-host that the metadata is reachable and that an unauthenticated
call is refused:

```bash
curl -fsS https://memory.example.net/.well-known/oauth-protected-resource/mcp
curl -s -o /dev/null -w '%{http_code}\n' https://memory.example.net/mcp
```

The first returns the protected resource metadata:

```json
{"resource":"https://memory.example.net/mcp","authorization_servers":["https://id.example.net/"],"scopes_supported":["athenaeum:read","athenaeum:write"],"bearer_methods_supported":["header"],"resource_name":"athenaeum"}
```

The second must print `401`. If it prints anything in the 2xx range, stop and fix the
configuration before registering the endpoint anywhere — an unauthenticated 200 on
that path means the whole auth block did not take effect.

Note that only the **path-scoped** metadata URL exists when the resource URL carries a
path: `/.well-known/oauth-protected-resource` with nothing appended is a 404, by
design, and is not a sign of a broken deployment.

### Client certificates are defense in depth, not authorization

Some hosts — OpenAI's connectors among them — can present a client certificate when
calling your endpoint, and a proxy can be configured to require and verify one.
Doing so is worthwhile: it means a stranger who learns the URL cannot even complete a
TLS handshake, which removes most background scanning from your logs.

**It is not authorization, and must never be configured as though it were.** A client
certificate identifies the *host platform*, not the person or agent calling through
it — every tenant of that platform presents a certificate from the same issuer. It
says "this request came from that provider's infrastructure"; it says nothing about
who asked. Authorization stays where it is: the verified token, the identity
allowlist, the scope split, and the pinned audience. Configure mTLS as an extra
outer layer, keep every athenaeum auth setting exactly as it would be without it, and
never widen the allowlist because mTLS is on.

## Keeping the corpus fresh on a server

On a workstation, Claude Code's hooks compile and reindex around each session. **No
such hook exists on a server.** Nothing a remote agent `remember`s becomes recallable
to anyone else, and nothing you add to the store becomes visible at all, unless a
scheduler runs the batch path.

Three jobs, in dependency order:

```bash
athenaeum run --path ~/knowledge             # nightly: the full librarian compile
athenaeum session-end --path ~/knowledge     # often: incremental ingest, then reindex
athenaeum reindex --path ~/knowledge         # on demand: index only, no compile
athenaeum ingest-answers --path ~/knowledge  # periodically: drain the resolved queue
```

`athenaeum run` is the batch compile that turns `remember`ed intake into wiki pages;
`athenaeum reindex` is what makes those pages visible to `recall`. Neither is implied
by the other, and a store that is compiled but not reindexed answers every `recall`
with nothing and no error. `athenaeum reindex --full` is for seeding or after an
embedding-model change; the default incremental hash-diff is what a schedule wants.

`session-end` is the one to schedule frequently. It is change-gated — an incremental
ingest, then a reindex only when the compile ran or the index is actually stale — so a
tick with nothing new costs no model calls and no index work. It is what closes the
write-then-recall gap for a remote caller, and running it every few minutes is
cheaper than running `reindex --full` once.

Two details specific to remote mode:

- **A remote `remember` lands in `raw/remote-session/`**, not `raw/claude-session/`.
  `athenaeum init` creates it and registers it as a `recall.extra_intake_roots`
  default, so a note written there is recallable in the same session as the write.
  If you are scheduling against an older store, confirm that root exists before
  assuming a remote write is being picked up.
- **Wiki-level sources default to `remote:inferred`** under `--transport http`, not
  `claude:inferred`. That is the string to select on when you want to know what
  arrived over the remote endpoint.

A systemd service plus a timer is the shape that survives a reboot. Adjust the paths
to wherever your own virtualenv and store actually live:

```ini
# /etc/systemd/system/athenaeum-serve.service
[Unit]
Description=athenaeum MCP server (streamable HTTP, loopback bind)
After=network-online.target

[Service]
Type=simple
User=athenaeum
Environment=ATHENAEUM_CACHE_DIR=/opt/athenaeum/cache
ExecStart=/opt/athenaeum/venv/bin/athenaeum serve --path /opt/athenaeum/knowledge --transport http --profile remote
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/athenaeum-session-end.service
[Unit]
Description=athenaeum incremental ingest + reindex

[Service]
Type=oneshot
User=athenaeum
Environment=ATHENAEUM_CACHE_DIR=/opt/athenaeum/cache
ExecStart=/opt/athenaeum/venv/bin/athenaeum session-end --path /opt/athenaeum/knowledge
```

```ini
# /etc/systemd/system/athenaeum-session-end.timer
[Unit]
Description=Run athenaeum session-end every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

The nightly compile is the same pattern with `ExecStart=.../athenaeum run --path
/opt/athenaeum/knowledge` and `OnCalendar=03:30`. The `serve` unit and the scheduled
units must run as the **same user** and share one `ATHENAEUM_CACHE_DIR`, or the
server reads an index the compiler never wrote.

If you prefer cron, the equivalent two lines, with the same user and the same cache
directory in the environment:

```bash
*/5 * * * * /opt/athenaeum/venv/bin/athenaeum session-end --path /opt/athenaeum/knowledge
30 3 * * *  /opt/athenaeum/venv/bin/athenaeum run --path /opt/athenaeum/knowledge
```

`run` needs model credentials in its own environment (`session-end`'s incremental
ingest can compile pre-structured intake with no model call at all, but will need
them for anything else). `serve` needs none — it only reads and writes files. Put the
key in the compile unit's environment, not the server's, and the server process never
holds a credential it cannot use.

Both `run` and `session-end` are single-flight on one run lock, so overlapping ticks
are safe: the second exits rather than racing. Both print a one-line JSON summary and
exit non-zero on failure, which is what makes them worth alerting on.

## Logs

There is no athenaeum log file. Every line goes to stdout/stderr, so your service
manager owns the log — `journalctl -u athenaeum-serve` under the units above.

What you get, and what you do not:

- **A `[warn] search_backend=... but no index at <path>` line at startup means
  `recall` will return nothing.** It is the single most common silent remote-mode
  failure: the server resolved a different `ATHENAEUM_CACHE_DIR` than the compiler
  wrote to. Fix the cache directory, do not ignore the warning — nothing downstream
  reports it again.
- **Startup lines name the whole resolved configuration**: the `[auth]` line carries
  the issuer, the audience and the allowed-identity count; `[remote-policy]` carries
  the profile and the excluded-fields pin; `[transport]` carries the bind and the
  path prefix. These are the lines to diff against your intent after a config change,
  and they are the only place the resolved values appear.
- **No token is ever logged**, in whole or in part, by athenaeum or by the verifier
  underneath it. An allowlist refusal names the `sub` it refused — a value from your
  own configuration — and nothing else.
- **A rejected request is a 401 with a `WWW-Authenticate` challenge** naming the
  metadata URL and the required scopes. An identity that is not on the allowlist gets
  the same 401 an invalid signature gets, deliberately: a distinguishable 403 would
  let a caller enumerate which subjects you trust. So "401" alone does not tell you
  which of the two happened — read the refusal line in the log.
- **`raw/remote-session/` is the durable record of what a remote agent wrote.** Every
  `remember` is one append-only file there, with its own provenance. It is more
  useful than any log line for answering "what did that host put in my store".
- **The local push-metrics ledger records that a `recall` happened**, as a
  best-effort telemetry append — counts and hashes, on your own disk. Useful for
  "is this endpoint being used"; not an access log, and not a record of what was
  returned.
- **What was returned is not recoverable from your side.** The page text went to the
  host provider. Their retention is the only place it is logged. This is the same
  point as [Every recall result is egress](#every-recall-result-is-egress), stated
  where you would go looking for an audit trail.

Keep the proxy's access log too. It is the only place a request that never reached
athenaeum — a TLS failure, a path typo, a scanner — appears at all.

## Rotation and revocation

athenaeum holds no credential, which makes most of this short: there is nothing of
yours to rotate. What there is:

- **Signing keys rotate at the provider, with no athenaeum action.** The key set is
  fetched from the issuer and cached by key id, so a provider that publishes the new
  key before signing with it rotates transparently. If your provider rotates without
  overlap, expect a window of rejected tokens and plan a restart around it.
- **Revoking one identity is an allowlist edit plus a restart.** Remove the `sub` or
  email from `serve.auth.allowed_identities` and restart the server. The pins are
  resolved once, at startup, and fixed for the life of the process — there is no
  reload signal, by design: a serve-time pin that could change mid-process is a pin
  you cannot reason about. `systemctl restart athenaeum-serve` is the revocation
  mechanism.
- **Revoking at the provider is the faster half, and the incomplete one.** Disabling
  the account or revoking the grant stops new tokens immediately, but an access token
  already issued stays valid until it expires — athenaeum verifies signature,
  issuer, audience, identity and scope, and does not call a token-introspection
  endpoint. Keep access-token lifetimes short at the provider; that lifetime is your
  worst-case revocation delay.
- **Cutting off everything is one command.** Stop the server, or stop the proxy. With
  the bind on loopback, stopping the proxy makes the endpoint unreachable without
  touching athenaeum at all.
- **Rotating the TLS certificate is purely a proxy operation.** athenaeum does not
  read it, so there is nothing to restart on the athenaeum side.
- **Changing `serve.auth.resource` invalidates every outstanding token** and requires
  re-registering the connector at every host. It is the audience those tokens were
  minted for. Treat the resource URL as permanent; pick it once.
- **What revocation cannot reach is what was already recalled.** See
  [Every recall result is egress](#every-recall-result-is-egress). Revoking access
  stops the next read; it does not retrieve the last one.

## Do not

- **Do not bind a non-loopback address without authentication.** With no issuer
  configured, athenaeum refuses it outright and exits before anything binds — that
  refusal is a feature, not an obstacle to route around. Do not route around it with
  a tunnel, an SSH forward, a stdio-to-HTTP shim or a container port mapping either.
  Any of those publishes an unauthenticated server with full read and write access to
  your entire knowledge store to whoever finds the URL, and URLs are found.
- **Do not serve the owner audience by default.** Leaving `serve.audience` unset means
  owner, which means every page in the store — untagged, `confidential` and
  `personal` included — is in scope for a remote host. Pin the narrowest audience
  that makes the deployment useful, and widen it deliberately, later, with a reason.
  Owner-by-default is the single costliest misconfiguration available here.
- **Do not set `excluded_fields: on` without understanding where the values go.**
  `on` lets a remote caller resolve excluded, off-corpus fields — the contact data
  deliberately kept off entity pages. Every value it returns goes to the host
  provider, with everything in
  [Every recall result is egress](#every-recall-result-is-egress) applying to it in
  full, and that class of data is exactly what the excluded surface exists to keep
  out of a corpus in the first place. The `remote` profile pins it `off`. Turning it
  back on is a decision about a third party's retention policy, not a convenience
  toggle.
- **Do not treat a client certificate as authorization.** See
  [above](#client-certificates-are-defense-in-depth-not-authorization).
- **Do not reuse one process for two audiences.** One process, one `resource`, one
  pinned read scope. Run a second process on a second port and name.
- **Do not point the server at an index a different user wrote.** A cache directory
  the serving user cannot read yields empty `recall` results and no error.
- **Do not skip the unauthenticated-request check.** Confirm `401` from off-host,
  every time the proxy or the auth block changes. It is the one test that proves the
  thing you actually care about.

## See also

- Guides — [Other clients and platforms](clients.md) · [Install](install.md) · [Day-to-day operation](daily-operation.md) · [Troubleshooting](troubleshooting.md)
- Modules — [mcp](../modules/mcp.md) · [sensitivity](../modules/sensitivity.md) · [recall](../modules/recall.md)
- Design — [ChatGPT host and remote mode](../design/host-chatgpt.md) · [security posture](../design/security-posture.md)
- Reference — [configuration](../reference/configuration.md) · [CLI](../reference/cli.md)
