<!-- SPDX-License-Identifier: Apache-2.0 -->

# ChatGPT as an athenaeum host, and the remote mode it needs

**Status:** RATIFIED 2026-11-11. Epics athenaeum#2200 (ChatGPT host) and athenaeum#2199
(authenticated remote mode, the shared prerequisite). The operator ruled on every
decision in [§8](#8-open-decisions-for-the-operator) (athenaeum#2378,
athenaeum#2379). The operator's adoption order (Claude Code marketplace, then Cursor, then remote mode and
ChatGPT, then Grok) still applies: nothing here starts ahead of that order.

Companion to [`security-posture.md`](security-posture.md) (audience scoping, the
outbound-LLM PII posture) and
[`sidecar-adapter-contract.md`](../extending/sidecar-adapter-contract.md) (the
three-tier host taxonomy this page uses to say what "auto-recall" can mean).

Research for this page was done on 2026-11-10 against OpenAI's public developer
documentation. ChatGPT's product surface changes often, and OpenAI has renamed it
more than once (connectors, then apps, then plugins). Every claim in §2 is marked
**verified** (read on a primary OpenAI page, URL given) or **inferred** (secondary
source, or an absence of documentation). Re-check the inferred ones before building
on them.

## 1. What exists today

### 1.1 The Claude Code host

Three pieces, and only one of them is Claude-specific:

| Piece | Where | Host-specific? |
|---|---|---|
| MCP server | `athenaeum serve` → `_cmd_serve.py:cmd_serve` → `mcp_server.py:create_server` → `server.run()` with no transport argument, so stdio only. | No. Any stdio MCP client can launch it ([clients guide](../guides/clients.md)). |
| Per-turn auto-recall | `athenaeum-claude-hook` (`claude_code_adapter.py:main`) on Claude Code's `UserPromptSubmit` event. A thin Tier-1 adapter over `context.py:build_context_for_turn`. | The adapter is. The context builder it wraps is host-neutral: the Tier-2 Codex adapter (`athenaeum codex-context`, [Codex guide](../guides/codex.md)) reuses the same `build_context` core. |
| Install path | Hook kit under `examples/claude-code/`, MCP registration per [install guide](../guides/install.md). The SessionStart hook also rebuilds the search index. | Yes. |

### 1.2 What the MCP server assumes about its process

`create_server` registers 15 tools (`recall`, `entity_schema`, `enumerate_entities`,
`remember`, `list_pending_questions`, `resolve_question`, `raise_decision`,
`list_pending_merges`, `list_pending_decisions`, `read_entity`, `list_axiom_audit`,
`scan_retraction_cascade`, `calibration_summary`, `review_audit_item`,
`resolve_merge`; the module docstring's Reads/Writes list omits `raise_decision`). Five
assumptions about the process are baked in, and an HTTP transport breaks or stresses
each of them:

1. **One audience per process.** `caller_audience` is a `create_server` argument,
   pinned once and closed over by every tool. It is deliberately not a per-tool
   argument, so a restricted caller cannot widen its own scope
   (`mcp_server.py` module docstring; [security posture §2.1](security-posture.md)).
2. **One conversation per process.** The comment above `_session_resolver` in
   `create_server` says a stdio server "is spawned once per conversation", and that is
   what makes the `ToolUseSessionResolver` caches sound.
3. **Claude-shaped attribution.** `active_tool_use_id` reads the
   `claudecode/toolUseId` request `_meta` key. ChatGPT will not send it. The function
   already returns `None` in that case, so this degrades rather than fails.
4. **Claude-shaped provenance defaults.** `remember` defaults `source` to
   `"claude-session"` and, when no `sources` is passed, stamps
   `_DEFAULT_INFERRED_SOURCE = "claude:inferred"`. `_resolve_inferred_session_id`
   reads `CLAUDE_CODE_SESSION_ID` and already documents that under an HTTP or shared
   server it should stamp nothing. A ChatGPT write would still be labeled as a Claude
   inference, which is wrong.
5. **One client at a time.** Under stdio, `remember` writes and the
   `_refresh_extra_root_index` call it triggers never race a concurrent `recall`
   from another client. Under HTTP they can.

`create_server` must also stay free of corpus-proportional scans
(athenaeum#1194). A remote cold start sits behind a TLS handshake, an OAuth
round trip, and, on the tunnel path, a 120-second request deadline.

## 2. ChatGPT's capabilities (researched 2026-11-10)

### 2.1 Verified

- **Custom MCP servers are added from ChatGPT's Plugins page** ("Add custom MCP
  server", choose a connection, configure authentication, "Create as a plugin"). The
  endpoint should be public HTTPS speaking streamable HTTP, typically at `/mcp`.
  Tool metadata is re-read on an explicit Refresh.
  <https://developers.openai.com/apps-sdk/deploy/connect-chatgpt>
- **Transport is MCP streamable HTTP.** Production servers must "Support the MCP
  streamable HTTP transport". SSE is not mentioned on that page.
  <https://developers.openai.com/apps-sdk/build/mcp-server>
  (The older deep-research example still uses an SSE endpoint ending in `/sse/`;
  <https://developers.openai.com/api/docs/mcp>. Build for streamable HTTP.)
- **Authentication is OAuth 2.1 per the MCP authorization spec.**
  <https://developers.openai.com/apps-sdk/build/auth>
  - Protected resource metadata (RFC 9728) at
    `/.well-known/oauth-protected-resource`, or advertised in `WWW-Authenticate` on a
    401.
  - Authorization server metadata (RFC 8414 or OIDC discovery) must advertise PKCE
    `S256`; servers that omit it are unsupported.
  - Client registration: Client ID Metadata Documents (CIMD) are preferred, and
    ChatGPT sends a stable `https://chatgpt.com/oauth/.../client.json` URL as its
    `client_id`. Dynamic client registration (DCR) is still supported.
  - ChatGPT sends a `resource` parameter; the authorization server should put it in
    the token audience. The resource server must verify signature, `iss`,
    `exp`/`nbf`, audience, and per-tool scopes, and answer failures with 401 plus
    `WWW-Authenticate`.
  - Machine-to-machine grants (client credentials, service accounts, JWT bearer) are
    not supported.
  - ChatGPT presents an OpenAI-managed mTLS client certificate the server may
    validate (SAN `mtls.prod.connectors.openai.com`).
  - Tools declare `securitySchemes` (`noauth` and/or `oauth2` with scopes).
- **Steering surfaces.** ChatGPT uses the server's `instructions` (returned at MCP
  initialization) alongside tool metadata. OpenAI advises putting the key details "in
  the first 512 characters". Tool names, descriptions, and annotations
  (`readOnlyHint`, `destructiveHint`, `openWorldHint`) are what "the model uses ... to
  decide whether and how to call the tool". Annotations guide confirmation behavior
  but "do not replace authorization, validation, or confirmation in your server."
  <https://developers.openai.com/apps-sdk/build/mcp-server>
- **Secure MCP Tunnel.** A customer-run `tunnel-client` long-polls OpenAI over
  outbound HTTPS and forwards MCP requests to a private server, which needs no
  inbound listener. The private server can be an HTTP URL (`--mcp-server-url`) **or a
  stdio command (`--mcp-command`)**. Requirements: an OpenAI Platform organization,
  a `tunnel_id`, a runtime API key, the tunnel associated with the target ChatGPT
  workspace, and tunnel roles. The OAuth authorization server is not tunneled. Limits:
  a 120-second request deadline, 10 MiB per message, and 600 requests per minute. It
  serves ChatGPT, Codex, and the Responses API, and cannot be used for public plugin
  distribution.
  <https://developers.openai.com/api/docs/guides/secure-mcp-tunnels.md>
- **Deep research and company-knowledge compatibility** wants two read-only tools,
  `search(query) -> {results: [{id, title, url}]}` and
  `fetch(id) -> {id, title, text, url, metadata?}`, with results in
  `structuredContent` and mirrored as JSON text. Citations are created only when `url`
  is a non-empty string. <https://developers.openai.com/api/docs/mcp>

### 2.2 Inferred or unverified

- **Which plans can add a custom MCP server, and whether writes are allowed.** OpenAI's
  help article (help.openai.com article 12584461) refused automated fetches (HTTP 403).
  Secondary sources from October 2026 consistently report: full MCP including
  write actions for Business and Enterprise/Edu (admins and owners only in Business);
  Pro limited to read/fetch; Plus and Free unclear or absent. Older guides said every
  paid plan could use Developer Mode. **Treat as unverified.** The practical
  consequence: on a Pro account `remember` may be unavailable, and on Plus the whole
  integration may be. Verify on the operator's real account before building
  (athenaeum#2200 children).
- **No per-turn hook exists.** No OpenAI page describes a mechanism that runs
  server-side code, or injects server-supplied context, before every ChatGPT turn.
  This rests on that absence across the Apps SDK, connector, and tunnel docs. It is
  not an explicit statement.
- **No third-party write path into ChatGPT memory.** No API for an MCP server or app to
  read or write ChatGPT's saved memories is documented. Inferred from absence.
- **Custom GPTs use Actions (OpenAPI), not MCP.** Secondary sources only, and one says
  a single Custom GPT cannot combine Actions with MCP apps.
- **No-auth servers.** The `noauth` security scheme exists, and secondary guides
  describe a no-auth option in the connect flow. A personal corpus must not use it
  (§5), so this is moot except for loopback testing with MCP Inspector.
- **Mobile.** Secondary sources say custom MCP plugins are web-only. Unverified.

## 3. Architecture

### 3.1 The repo-split call

athenaeum becomes an **OAuth resource server only**. It verifies tokens and maps a
verified identity to an audience. It never issues tokens, stores passwords, or
terminates public TLS. The authorization server (any OIDC provider that advertises
PKCE `S256` and CIMD or DCR), the TLS-terminating reverse proxy, the domain, and the
host all belong to the **deployment**, outside this repository, per the
deployment-boundary rule in `AGENTS.md`. This repo ships the mechanism, tests against
an in-process fake issuer, and a deployment guide with invented hosts
(`memory.example.net`, `id.example.net`).

### 3.2 Components

```
ChatGPT ──HTTPS──► reverse proxy (TLS, deployment) ──► athenaeum serve --transport http
   │                                                     │  ├─ token verifier (JWKS, iss, aud, exp, scope)
   │                                                     │  ├─ identity → audience (one process per audience)
   │                                                     │  ├─ remote profile pins (tool allowlist, with_pii off)
   └──OAuth (PKCE S256, CIMD/DCR)──► authorization server (deployment)
                                                         └─ same create_server(), same 15 tools minus the disabled ones
```

- **Transport (athenaeum#2199).** `athenaeum serve --transport http --host --port`
  runs FastMCP's streamable HTTP transport. stdio stays the default and stays
  byte-identical. Without auth configured, the server refuses to bind anything but
  loopback, so MCP Inspector works locally and nothing else does.
- **Auth (athenaeum#2199).** FastMCP's resource-server auth (JWT verification against
  a configured issuer's JWKS) serves the RFC 9728 metadata and the 401 challenge. The
  floor of the `mcp` extra's `fastmcp` pin (currently `>=2.0`) rises to the first
  release with the needed auth surface. The build child names that version.
- **Identity → audience.** v1 keeps assumption 1 intact: **one server process per
  audience**. The operator maps an allowlisted identity (a `sub` or a verified email)
  to the process's pinned audience. A token whose identity is not on the allowlist gets
  a 401 even if it is otherwise valid. Per-request audience from token claims, which
  would mean refactoring the `caller_audience` closure, is deferred to a team-mode
  follow-up. That is a breaking change to the security model and needs its own design.
- **Remote profile pins.** New serve-time pins, resolved like `screening` and
  `audience` (CLI > env > yaml) and never offered as per-tool arguments:
  - a tool allowlist, where disabled tools are absent from `tools/list` rather than
    merely refusing;
  - excluded fields off: `recall(with_pii=True)` and `read_entity` never return excluded
    values in this process (§5.2);
  - owner-only decision-queue mutators (`resolve_question`, `resolve_merge`,
    `review_audit_item`) disabled.

  These pins are transport-independent, so they are testable over stdio too.
- **Tool annotations (host-neutral).** Every tool gets accurate `readOnlyHint`,
  `destructiveHint`, and `openWorldHint` values. No tool has any today. Reads are
  `readOnlyHint: true`. `remember` and `raise_decision` append, so they are not
  destructive. `resolve_*` and `review_audit_item` mutate the decision queue. Every
  tool is `openWorldHint: false`, because the corpus is a bounded private store.
- **Long-lived-process hygiene.** Make `remember`'s default provenance host-neutral
  when no Claude session is present. Prove that concurrent `remember`, index refresh,
  and `recall` are safe, or serialize them. Confirm `ToolUseSessionResolver` and push
  telemetry are inert when `active_tool_use_id` is `None`.

### 3.3 The tunnel path, and why it is not the core

Because Secure MCP Tunnel accepts a stdio command, `tunnel-client --mcp-command
"athenaeum serve ..."` could put the current server in front of ChatGPT with no
HTTP code at all. This is attractive for a single-owner deployment, and worth a
cheap `~operator` spike. It is not the 2199 core, for four reasons:

- It is ChatGPT- and OpenAI-only. It does nothing for Grok (athenaeum#2201) or other
  remote clients, so it is not a shared prerequisite.
- It needs an OpenAI Platform organization, an API key, and workspace association.
  One community report says Business-workspace association can fail with no manual fix.
- Its access control is ChatGPT workspace membership, not an identity athenaeum can
  verify. Whether ChatGPT still demands OAuth for a tunnel connection is undocumented.
- Process lifetime under `--mcp-command` (one process per request, or one long-lived
  process) is undocumented. Assumptions 2 and 5 depend on it.

## 4. What "auto-recall" can honestly mean on ChatGPT

In [the sidecar contract's taxonomy](../extending/sidecar-adapter-contract.md#3-the-three-tier-host-taxonomy),
Claude Code is **Tier 1** (per-turn push) and Codex is **Tier 2** (static context
file). ChatGPT exposes no per-turn injection point (§2.2), so an MCP connection
there is **Tier 3, pull only**. The contract names Tier 3 explicitly as *not the
sidecar*, so that exposing retrieval over MCP is never mistaken for having
generalized unprompted push.

So ChatGPT gets **steered recall**, not auto-recall: the model is told, as
persuasively as the verified levers allow, to call `recall` before answering, and
it decides whether to. The levers, strongest first:

1. **Server `instructions`** (verified as consumed by ChatGPT). Today's string is
   inline in `create_server` and opens with `remember`. A remote-profile variant leads
   with recall guidance inside the first 512 characters. It is text a host's model
   reads, like the recall overflow breadcrumb, so it follows that precedent: a packaged
   template under `src/athenaeum/prompts/` (as `recall_overflow_breadcrumb.md` is), not
   an inline literal. It is not one of the prompts athenaeum sends to its own model,
   which [`prompts.md`](prompts.md) inventories. It is served only by the
   remote profile, so the Claude Code host, which already has Tier-1 push, does not
   change.
2. **Tool description and annotations** on `recall` (verified as used for tool
   selection). Annotating `recall` read-only also lowers confirmation friction.
3. **User-side instructions** (ChatGPT custom instructions or project instructions,
   standing product features). The operator pastes a line such as "Before answering
   anything about my work or people I know, call the athenaeum `recall` tool." This
   lever is the user's, not athenaeum's, and the setup guide documents it.
4. **A Tier-2 analogue**: uploading a periodic `codex-context`-style snapshot as a
   project file. This is manual and stale, and is documented as an option only.

**Not available, by the evidence in §2.2:** a pre-turn hook, injecting into ChatGPT
memory, or a Custom GPT wrapping the MCP server.

**The bar.** athenaeum does not label any of this "auto-recall" in docs or the client
table unless a measurement shows it. A ChatGPT child measures the steered pull rate
(the share of turns where the model called `recall` when the corpus had a relevant
answer) against the Claude Code hook baseline on the same eval prompts. The result
lands in `docs/measurements/`. Until then, [the clients table](../guides/clients.md)
says "explicit and steered tool calls; no auto-recall".

## 5. Security model for a personal corpus over HTTPS

### 5.1 Authentication and authorization

- **No unauthenticated non-loopback bind, ever.** The existing warning in
  [the clients guide](../guides/clients.md) against putting a stdio-to-HTTP proxy
  behind a public tunnel stays, and the code enforces it: `--transport http` with a
  non-loopback `--host` and no issuer configured exits non-zero.
- **The audience is bound to the authenticated identity, not to a client flag**
  (§3.2), and one process serves one audience. A valid token for an identity not on
  the allowlist is rejected. A token whose audience is not this server's canonical
  resource URL is rejected.
- **Scopes gate tool classes.** A read scope covers the read tools and a write scope
  covers `remember` and `raise_decision`. The owner-only mutators are not exposed
  remotely in v1 at all.
- **Optional defense in depth.** The deployment can require OpenAI's mTLS client
  certificate at the proxy. That narrows callers to ChatGPT but authorizes no one, so
  it complements OAuth and never replaces it.

### 5.2 PII and the `with_pii` path

Excluded fields (a person's contact data, and anything else the operator routes off
the corpus) are read through exactly one path: `recall(with_pii=True)` or
`read_entity` ([security posture §2.1](security-posture.md)). Over a remote host,
every value that path returns is sent to a third-party model provider and kept in
that provider's conversation history.

[Security posture §2.2](security-posture.md) records L6 as "the outbound path is not
a PII boundary", by design. That decision was made for the operator's own model calls
from the operator's own process. A ChatGPT connection is a different egress: a
third-party host, retained conversation history, and model output the operator does
not control. So this design proposes that **the remote profile disables the excluded
field path by default**:

- `with_pii=True` is accepted but returns a withheld notice in place of excluded
  values.
- `read_entity` returns no excluded values.
- `enumerate_entities` refuses PII-scoped predicates, as it already does without
  `with_pii`.

Turning it on is an explicit serve-time pin. This is decision D4.

The audience filter still applies first. With a restricted audience pinned (D3),
`confidential` and `personal` pages are withheld before the PII question arises.

### 5.3 Egress

Every recall result is egress to the host. The remote process should run with the
narrowest audience the operator accepts, and the deployment guide states plainly that
anything recall returns leaves the machine. Write egress is the reverse risk:
`remember` lets a third-party model put content into the corpus. That content still
passes intake screening (athenaeum#320) and compiles through the normal path, and it
must carry host-accurate provenance (a ChatGPT source, never `claude:inferred`) so
trust and precedence can tell it apart.

## 6. Failure classes the design must survive

| Failure | Survives by |
|---|---|
| Server exposed without auth | Refusing to bind non-loopback without an issuer, plus a test pinning that refusal. |
| Token for the wrong resource, or a forwarded token | Audience check against the canonical resource URL. |
| Valid token, wrong person | Identity allowlist → 401. |
| Restricted remote caller widening scope | Audience pinned per process. There is still no per-tool audience argument. |
| Excluded values leaking to a third-party host | The remote profile disables the `with_pii` path by default. |
| A model approving a destructive call | Owner-only mutators are absent from the remote `tools/list`, not just refused. |
| Concurrent `remember` and `recall` corrupting the index | The concurrency child either proves safety or serializes. |
| Cold start exceeding a client or tunnel deadline | `create_server` stays scan-free (athenaeum#1194), plus a startup-latency check in the smoke test. |
| ChatGPT's plan gating or protocol changes | Live verification children record the date and plan. The docs carry that date. |
| Stale index on a headless server | The deployment guide schedules `athenaeum run` and reindex. No Claude SessionStart hook exists on a server. |

## 7. Migration and rollback

Remote mode is additive and off by default. stdio behavior, the default tool set, and
the Claude Code `instructions` string are unchanged. Rollback is to stop running
`--transport http`. No data format changes. The only persistent effect is `remember`
writes made remotely, which are ordinary raw intake with host-specific provenance and
can be found and retired by that source.

## 8. Open decisions for the operator

**Ruled 2026-11-11.** D1 to D8 were accepted exactly as recommended below (D1 to D5
on athenaeum#2378, D6 to D8 on athenaeum#2379). For D6 the operator added that
"auto-recall" is reserved for per-turn push on every host, so Grok uses the same
"steered recall" wording. D9: the operator holds no ChatGPT plan today and will buy
one, at most Pro or Business/Team, as the first step of the live spike
(athenaeum#2380). Business/Team is the minimum if write tools must be tested. Pro
supports only a read-only spike. The deep-research shim (athenaeum#2388) is parked.

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Un-park remote mode, and which path | (a) native streamable HTTP + OAuth resource server; (b) Secure MCP Tunnel only; (c) both | **(a) as the 2199 core**, with (b) as a cheap ChatGPT-only `~operator` spike under 2200. Only (a) serves Grok and other remote hosts. Timing follows the adoption order. |
| D2 | Who issues tokens | (a) external OIDC provider, athenaeum as resource server; (b) athenaeum embeds an authorization server; (c) a static bearer token | **(a).** (b) puts credential storage and consent UI into a memory library. (c) is not an OAuth flow ChatGPT will run. |
| D3 | Audience of the remote process | (a) owner (full access); (b) a restricted audience the operator names, with owner as an explicit opt-in | **(b).** Start narrow. The owner can opt in deliberately, but it is never the default for a third-party host. |
| D4 | Excluded fields (`with_pii`) remotely | (a) disabled by default, opt-in pin; (b) enabled, as on stdio | **(a)**, for the egress reasons in §5.2. |
| D5 | Remote write tools | (a) `remember` + `raise_decision` on, `resolve_*`/`review_audit_item` off; (b) read-only; (c) all | **(a)**, with host-accurate provenance. A plan that only allows read/fetch makes this moot for that plan. |
| D6 | What to call ChatGPT recall | (a) "steered recall", not auto-recall, until measured; (b) call it auto-recall | **(a).** The sidecar contract says Tier 3 is not the sidecar. |
| D7 | Scope of the steering `instructions` | (a) remote profile only; (b) change the shared string for every host | **(a).** It leaves the Claude Code host and its evals untouched. |
| D8 | Deep-research `search`/`fetch` shim | (a) defer; (b) build behind the remote profile | **(a)**, until the steered-recall measurement shows the regular tools are not enough. Citations also need a non-empty `url`, and corpus pages have none. |
| D9 | Target ChatGPT plan | Whatever the operator holds, verified live | **Verify first.** If the operator's plan cannot add a custom MCP server, or only allows read/fetch, the plan decides whether the 2200 epic proceeds at all. |

## 9. Rejected alternatives

- **A stdio-to-HTTP proxy behind a public tunnel, with no auth.** This exposes the
  whole store, including withheld pages, to anyone who finds the URL. It was already
  documented as "do not" and stays so.
- **An authorization server embedded in athenaeum.** It is out of the engine's remit,
  adds a credential store and consent UI to a library, and every deployment already
  has better identity providers. FastMCP's resource-server role is enough.
- **A Custom GPT Actions gateway (MCP → OpenAPI).** Actions are a separate,
  OpenAPI-shaped surface, MCP in Custom GPTs is unverified, and a gateway is a second
  server to secure for no gain over a direct MCP plugin.
- **Per-request audience from token claims in v1.** It is the right shape for team
  mode, but it rewrites the "pinned once per process" invariant every scoped read
  relies on. One process per audience preserves that invariant at the cost of one
  process per identity class, which is fine for a personal deployment.

## 10. Children

The child list is maintained in the bodies of athenaeum#2199 and athenaeum#2200, which
are the canonical index. This page does not duplicate it, so the list cannot drift.
