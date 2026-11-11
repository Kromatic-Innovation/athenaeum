# The MCP surface

**Reference page.** For the task-shaped version, see
[Passive recall via hooks](../guides/sidecar.md) and
[Claude Code integration](../guides/claude-code.md).

## What it does

`athenaeum serve` runs an MCP server exposing **15 tools** so agents can write to raw
intake, search the compiled wiki, and triage the human-decision queue.
9 read-only, 6 that mutate human-decision state.
No tool writes a wiki page directly — the librarian is the only writer.

```bash
pip install 'athenaeum[mcp]'
athenaeum serve --path ~/knowledge
athenaeum test-mcp          # smoke-test the round-trip without a live session
```

| Tool | R/W | What it does |
|---|---|---|
| `recall` | READ | Searches the compiled wiki for pages relevant to a query (keyword/FTS5/vector, depending on the configured backend). |
| `entity_schema` | READ | Reports the entity classes this deployment declares and observes, the fields each carries, and which fields `recall`/`enumerate_entities` can filter on. Call this before narrowing by `type`. |
| `enumerate_entities` | READ | Unranked, criteria-based listing of every entity of a declared type — the counterpart to `recall` for "every entity matching X", where a ranked search returns an incomplete answer. |
| `read_entity` | READ | One-call entity read by uid, for any entity class. One of the two sanctioned paths to a page's excluded fields. |
| `list_pending_questions` | READ | Unanswered contradiction-detector questions. |
| `list_pending_merges` | READ | Unresolved resolver-proposed page merges. |
| `list_pending_decisions` | READ | Unified queue — questions and merge proposals in one call, oldest first. |
| `list_axiom_audit` | READ | Per-slug history of axiom promotions and demotions, so axiom status is auditable without a write tool. |
| `scan_retraction_cascade` | WRITE | Flags completed merges that relied on a since-retracted source by appending a review item to the decision queue. Never auto-unmerges. |
| `calibration_summary` | READ | Per-tier sampled/reviewed/overturned counts for the tiered-reasoning calibration loop. Reports "not enabled" when the opt-in is off. |
| `remember` | WRITE | Appends a piece of knowledge to raw intake. Append-only; compiled into the wiki on the next run. |
| `raise_decision` | WRITE | Files a new agent-raised question or confirmation into the pending-decisions queue, so a mid-session flag has somewhere durable to live. |
| `resolve_question` | WRITE | Flips a pending question to answered and records the answer body. |
| `resolve_merge` | WRITE | Approves or rejects a pending merge proposal. Approval folds or creates the merged wiki page. |
| `review_audit_item` | WRITE | Records a human's confirm/overturn verdict on a sampled tier-audit item. Calibration signal only; never re-executes a merge. |

## Transport

By default `athenaeum serve` speaks MCP over **stdio**: the client launches the process and
talks to it over that process's own stdin and stdout. Nothing is bound and nothing is
reachable from a network. This is the shipped behaviour and it is unchanged.

`--transport http` opts into FastMCP's **streamable HTTP** transport instead, for hosts that
cannot launch a local process:

```bash
athenaeum serve --path ~/knowledge --transport http --port 8000
# [transport] streamable HTTP on http://127.0.0.1:8000/mcp
```

| Flag | Default | Applies to | What it sets |
|---|---|---|---|
| `--transport` | `stdio` | both | `stdio` or `http`. |
| `--host` | `127.0.0.1` | http | The bind address. Loopback only — see below. |
| `--port` | `8000` | http | The TCP port. |
| `--path-prefix` | `/mcp` | http | The URL path the endpoint is mounted at. |

`--transport`, `--host` and `--port` each resolve **CLI flag > environment variable >
`athenaeum.yaml`**, the same order every other knob uses: `ATHENAEUM_SERVE_TRANSPORT`,
`ATHENAEUM_SERVE_HOST` and `ATHENAEUM_SERVE_PORT`, then `serve.transport`, `serve.host` and
`serve.port`. `--path-prefix` is CLI-only. Under `stdio` the other three are inert, because
nothing binds. See [configuration](../reference/configuration.md) and
[the CLI reference](../reference/cli.md).

**TLS is not athenaeum's concern.** Terminate HTTPS at a reverse proxy in front of the
process; the server itself speaks plain HTTP to that proxy over loopback. For the
end-to-end deployment this implies — the proxy, the authorization server's own
requirements, scheduling the compile on a server, logs, rotation and revocation — see
[Remote mode](../guides/remote.md).

### What changes under `http`, and what does not

Under stdio, one process serves one conversation and one client, and the server is allowed
to assume that: the caller IS Claude Code, and the session id in the process environment
names the conversation being served. Under `http`, one process serves many conversations,
from whatever hosts registered the endpoint, for days. Three **defaults** follow the
transport as a result:

| | `stdio` | `http` |
|---|---|---|
| `remember`'s default `source` (the `raw/` subdirectory) | `claude-session` | `remote-session` |
| Defaulted wiki-level `source` when `sources` is omitted | `claude:inferred` | `remote:inferred` |
| Push-metrics attribution from the process environment when a request carries no `claudecode/toolUseId` | used | not used; nothing is recorded |

`raw/remote-session` is a `recall.extra_intake_roots` default and is created by
`athenaeum init`, so a note written there is indexed and recallable in the same session as
the write, exactly as `raw/claude-session` is.

The stdio column is byte-identical to the behaviour that shipped before this split, down to
the error strings — `claude:inferred` in particular must stay byte-identical because
`repair --backfill-sources` selects its candidates by equality on that scalar.

What does **not** change is the served surface. `tools/list` is identical on both
transports: the transport is a `create_server` argument, no tool gained a parameter, and
nothing in any tool's schema reveals which transport answered. An agent writes the same
calls either way.

Tracker for the multi-client work:
<https://github.com/Kromatic-Innovation/athenaeum/issues/2385>.

Concurrency is handled at one point: the post-`remember` index refresh re-reads the search
manifest, diffs the corpus and writes it back, so concurrent refreshes are serialized with
an in-process lock. Without it, two `remember` calls landing together each diffed the same
pre-image and the second dropped the first's entry — the file was on disk but invisible to
`recall`. The file writes themselves never needed serializing; each mints a unique name and
is renamed into place atomically.

## Authentication — a resource server, only

`athenaeum serve --transport http` can run as an **OAuth 2.1 resource server only**. It
verifies bearer tokens that an external authorization server issued, and that is the
entirety of its role in the protocol:

- It **never issues** a token, and has no token endpoint.
- It **never stores** a credential — no client secret, no refresh token, nothing at rest.
- It **never shows** a consent screen, and runs no authorization endpoint or dynamic client
  registration.

Everything on the issuing side — PKCE, client registration, consent, refresh — belongs to
the authorization server you point athenaeum at. If you do not already run one, athenaeum
is not the component that gives you one.

Configuring an issuer does two things at once, and that is deliberate: it is what permits a
non-loopback bind, and it is what makes every request to that bind carry a verified token.
There is no ordering in which you get one without the other.

```bash
athenaeum serve --path ~/knowledge --transport http --host 0.0.0.0 \
  --auth-issuer https://auth.example.com \
  --auth-resource https://memory.example.com/mcp \
  --auth-allowed-identities ops@example.com
# [auth] OAuth 2.1 resource server: issuer https://auth.example.com, audience
#        https://memory.example.com/mcp, 1 allowed identity, scopes
#        athenaeum:read athenaeum:write
# [transport] streamable HTTP on http://0.0.0.0:8000/mcp
```

| Flag | `athenaeum.yaml` | Environment variable | What it sets |
|---|---|---|---|
| `--auth-issuer` | `serve.auth.issuer` | `ATHENAEUM_SERVE_AUTH_ISSUER` | The authorization server's issuer identifier — what a token's `iss` claim must carry. |
| `--auth-resource` | `serve.auth.resource` | `ATHENAEUM_SERVE_AUTH_RESOURCE` | This server's canonical public URL — the one audience this process serves. |
| `--auth-allowed-identities` | `serve.auth.allowed_identities` | `ATHENAEUM_SERVE_AUTH_ALLOWED_IDENTITIES` | Comma-separated `sub` values, or emails the issuer marked verified. |
| `--auth-scopes` | `serve.auth.scopes` | `ATHENAEUM_SERVE_AUTH_SCOPES` | `read=<name>,write=<name>`. Defaults to `athenaeum:read` / `athenaeum:write`. |

Each resolves **CLI flag > environment variable > `athenaeum.yaml`**, the same order every
other knob uses. The first three are a set: configure all of them or none of them. Under
`stdio` the block is inert and says so on startup — stdio binds no socket and carries no
bearer token, so there is nothing to verify.

```yaml
serve:
  transport: http
  host: 0.0.0.0
  auth:
    issuer: https://auth.example.com
    resource: https://memory.example.com/mcp
    allowed_identities:
      - ops@example.com
      - auth0|operator-1
    scopes:
      read: athenaeum:read
      write: athenaeum:write
```

**The key set location is a convention, not a setting.** athenaeum reads the issuer's JSON
Web Key Set from `/.well-known/jwks.json` under the configured issuer, and accepts `RS256`
signatures only. That is the plain OIDC default, and it is what the hosted authorization
servers publish. An issuer that serves its key set from a tenant-scoped path instead is not
supported by this setting set — check your issuer's own discovery document before you
deploy, because the symptom is every token being rejected.

### What a client discovers

`GET /.well-known/oauth-protected-resource` serves RFC 9728 protected resource metadata:
the `resource` (your `serve.auth.resource`), `authorization_servers` (a one-element list
naming your issuer) and `scopes_supported` (both scope names). A resource URL that carries
a path is served at the path-scoped variant RFC 9728 §3.1 defines — `.../mcp` is published
at `/.well-known/oauth-protected-resource/mcp`.

Every rejected request answers **401** with a `WWW-Authenticate: Bearer` challenge naming
that metadata URL and the scopes to request, so a client can discover where to authenticate
without being told anything else.

### Refusals, and the reasons for them

- **The audience is this process's pin, and is never read from a token claim.** `serve.auth.resource`
  is set once, by the operator's own `athenaeum serve` invocation. It is what a token's `aud`
  claim is checked against, what the metadata advertises, and what a client passes as its
  `resource` parameter — one process, one audience. A token that presents an `audience`-like
  claim naming a broader role is served this pin anyway, and so is the separate read scope
  `--audience` pins: no claim in any token widens what `recall` returns.
- **An identity that is not on the allowlist is refused with the same 401 an invalid token
  gets.** Not a distinguishable 403 — a caller able to tell "your signature is fine but you
  are not on the list" from "your signature is wrong" can enumerate which subjects you
  trust. An empty allowlist is a startup error, never read as "anyone the issuer vouches
  for".
- **An unverified email claim is not an identity.** An `email` claim counts only when the
  same token carries `email_verified: true`; otherwise it is self-asserted by whoever
  registered the account.
- **A read-scoped token cannot call a write tool.** Each tool requires the scope matching
  its own `readOnlyHint` annotation (the table above): the read scope for the nine read
  tools, the write scope for the six that mutate. A token carrying neither can call
  nothing. The served `tools/list` is the same either way — authentication narrows who may
  call, never what is published.
- **A half-configured block refuses to start.** An issuer with no resource, or a resource
  with no issuer, exits non-zero before anything binds. Falling back to "no auth" is the
  one direction this must never fail in: it would publish an unauthenticated store on the
  address you asked for, while you believed it was protected.
- **No token reaches a log line.** Neither athenaeum's own refusals nor the underlying
  verifier's print a token or any segment of one. An allowlist refusal names the `sub` it
  refused — a value from your own configuration — and nothing else.

Still out of scope here, and genuinely elsewhere: TLS (above), validating a host's mTLS
client certificate (a deployment-proxy concern), and a per-request audience. For the latter,
run one process per audience.


## Tool annotations

Every tool carries an MCP `ToolAnnotations` hint so a remote host can decide
whether to call it unattended and when to ask the user for confirmation
(`docs/design/host-chatgpt.md` §3.2). Annotations are hints only — they
change no server-side behavior, which still enforces its own authorization
regardless of what a client infers from them. `openWorldHint` is `false` for
every tool: this deployment only ever reads/writes its own local knowledge
store, never an external service. A blank cell means the hint is unset — per
the MCP spec, `destructiveHint`/`idempotentHint` are meaningful only when
`readOnlyHint` is `false`, so the nine read-only tools never set either.

Tool name cells below are plain text, not code-spans — deliberately, so this
table is never mistaken by tooling for the R/W table above (whose rows are
matched by name/value pairs elsewhere in this repo).

| Tool name | readOnlyHint | destructiveHint | idempotentHint |
|---|---|---|---|
| recall | true | | |
| entity_schema | true | | |
| enumerate_entities | true | | |
| read_entity | true | | |
| list_pending_questions | true | | |
| list_pending_merges | true | | |
| list_pending_decisions | true | | |
| list_axiom_audit | true | | |
| calibration_summary | true | | |
| remember | false | false | |
| raise_decision | false | false | |
| scan_retraction_cascade | false | false | true |
| resolve_question | false | true | true |
| resolve_merge | false | true | true |
| review_audit_item | false | true | true |

Two side effects were found while auditing the nine read-only tools for
hidden state changes, and judged NOT disqualifying:

- `recall` appends a best-effort push-metrics telemetry record on every
  call. It is wrapped so a failure there can never surface as a `recall`
  failure, and it never touches wiki or decision-queue content — a local
  instrumentation side-channel, not a change to what `recall` or any other
  tool reads back.
- `enumerate_entities` and `read_entity` (when given a `cache_dir`) refresh a
  persisted search-index / entity-index cache file under that directory —
  the same derived, rebuildable plumbing `recall`/`rebuild-index` already
  maintain, never user-authored content.

One tool's side effect WAS disqualifying: `scan_retraction_cascade` durably
appends newly-flagged `(merge, retracted source)` pairs to
`wiki/_pending_retractions.jsonl`, which `list_pending_decisions` then
surfaces as `type: "retraction"` items — a real, user-visible change to the
decision queue, not a cache or telemetry side-channel. It is annotated as a
write (`readOnlyHint: false`), grouped with `remember`/`raise_decision` as
append-only and non-destructive (it never edits/removes an existing record
and never touches a completed merge), but — unlike those two — it is also
`idempotentHint: true`: a re-scan of unchanged state emits nothing new (see
the tool's own docstring).

## What it reads

- The compiled wiki at `<path>/wiki` (or `KNOWLEDGE_WIKI_PATH`) and its search index.
- The decision queues, `wiki/_pending_questions.md` and `wiki/_pending_merges.md`.
- The audience pinned at process start by `--audience`.

The raw and wiki roots default to `<path>/raw` and `<path>/wiki`. `KNOWLEDGE_RAW_PATH` and
`KNOWLEDGE_WIKI_PATH` override each root individually, while `--path` remains where
`athenaeum.yaml` and extra intake roots resolve:

```bash
KNOWLEDGE_RAW_PATH=/data/knowledge/raw \
KNOWLEDGE_WIKI_PATH=/data/knowledge/wiki \
  athenaeum serve --path ~/knowledge
```

## What it writes

- `remember` appends one file under `raw/<source>/` — `raw/claude-session` by default under
  stdio, `raw/remote-session` under `--transport http`. Nothing else touches `raw/`.
- The three decision-queue mutators edit the queue files and, for an approved merge, the
  merged wiki page.
- `scan_retraction_cascade` appends newly-flagged review items to
  `wiki/_pending_retractions.jsonl`, surfaced by `list_pending_decisions`. It never edits
  an existing record and never touches a completed merge.
- **No MCP tool writes an arbitrary wiki page.** An agent that wants a fact in the wiki
  calls `remember` and waits for the librarian.

## What it refuses

- **An HTTP server refuses every non-loopback bind until an auth issuer is configured.**
  This is a security property rather than a cautious default. With no issuer configured the
  HTTP transport carries no authentication of its own, so binding a routable address would
  publish the entire store to anyone who can reach the port. Configuring one (see
  [Authentication](#authentication--a-resource-server-only) above) lifts the restriction,
  because it is the same act that makes every request carry a verified token. The check is a POSITIVE allowlist of loopback address
  **literals** — any `127.0.0.0/8` address, the IPv6 loopback (bracketed or bare), and
  IPv4-mapped IPv6 loopback forms. Every other value exits non-zero **before any socket is
  bound**, and the message names the missing `serve.auth.issuer` setting rather than only
  reporting that the address is disallowed. Host *names* are never resolved, `localhost`
  included: resolving one would move the loopback decision into `/etc/hosts`, DNS and search
  domains, so a name that is loopback today could be routable tomorrow with nothing in
  athenaeum's own configuration having changed.

- **A restricted audience fails closed on reads.** `athenaeum serve --audience ops` pins a
  read scope for the life of the process. `recall`, `read_entity`, `enumerate_entities` and
  the three pending-decision list tools all apply the same predicate.

  **Three tools are not audience-scoped**: `list_axiom_audit`,
  `scan_retraction_cascade` and `calibration_summary` take no audience and apply no filter.
  They return slugs, paths and counts rather than page bodies, so a restricted caller learns
  no page *content* through them — but it can learn that a page **exists**. If that
  distinction matters to your deployment, do not expose those three to a restricted
  audience. Note that `scan_retraction_cascade` is not a read tool at all: it appends
  review items to the owner's decision queue (which is why it is annotated
  `readOnlyHint: false`, above), and unlike the three decision-queue mutators it is
  **not** refused for a pinned audience. Its annotation records that behaviour; it does
  not change it. Pages carry `access:`
  (`open`/`internal`/`confidential`/`personal`) and/or an `audience:` role list; **untagged
  pages are invisible** to a restricted caller. The owner, with no audience pinned, sees
  everything.
- **The audience cannot be widened by the caller.** It is an operator decision made at
  serve time.
- **A restricted process refuses the decision-queue mutators entirely.**
  `resolve_question`, `resolve_merge` and `review_audit_item` fail closed for any pinned
  audience — adjudicating the operator's decision queue is owner-only.
- **`calibration_summary` reports "not enabled"** rather than fabricating counts when
  reasoning-tier screening is off.
- **`scan_retraction_cascade` never unmerges.** It reports; a human decides.

This is a single-owner read filter, not a multi-user ACL.

## See also

- Guides — [Claude Code integration](../guides/claude-code.md) · [Sidecar](../guides/sidecar.md) · [Answering decisions](../guides/decisions.md) · [Remote mode](../guides/remote.md)
- Modules — [recall](recall.md) · [intake](intake.md) · [sensitivity](sensitivity.md)
- Design — [security posture](../design/security-posture.md) · [authorized reader contract](../extending/authorized-reader-contract.md)
- Reference — [configuration](../reference/configuration.md) · [exit codes](../reference/exit-codes.md)
