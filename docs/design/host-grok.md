<!-- SPDX-License-Identifier: Apache-2.0 -->

# Grok as an athenaeum host

**Status:** DESIGN OF RECORD (Vitruvius Specify, 2026-11-10). Scopes the epic
athenaeum#2201. The shared remote (HTTPS) prerequisite is owned by
athenaeum#2199 and is not designed here; this page states only what Grok
needs from it (§6).

Research date for every external claim: **2026-11-10**. xAI ships quickly; a
claim here older than about three months should be re-checked before it is
built on (§9).

---

## 1. Summary

"Grok" is three different hosts, and they do not share one answer.

| Surface | How it reaches MCP | Needs athenaeum#2199? | Best auto-recall available |
|---|---|---|---|
| **Grok Build** (xAI's terminal coding agent) | Launches **local stdio** servers, and also reads Claude Code and Cursor MCP config | **No** | Tier 2 snapshot plus an instruction pointer, at best. Per-turn push is **not available as documented** (§3.2). |
| **Grok apps** (grok.com, iOS, Android) | **Custom connector**: remote MCP over public HTTPS | **Yes** | Instructed pull only (Tier 3 with an instruction). No hook exists. |
| **xAI API** (Responses API, native SDK) | **Remote MCP tool**: xAI's servers call a public URL | **Yes**, or none at all if the caller injects context itself | Whatever the caller builds; out of scope for this epic (§7). |

**Verdict.** A meaningful integration is feasible today on Grok Build,
without remote mode: every MCP tool by explicit call, plus a periodic
snapshot. The Grok apps become feasible once athenaeum#2199 lands, with
explicit tools and instructed recall. True per-turn auto-recall, as athenaeum
means it on Claude Code (Tier 1 in
[the sidecar adapter contract §3](../extending/sidecar-adapter-contract.md#3-the-three-tier-host-taxonomy)),
is **not available on any Grok surface** as documented today. This epic must
not claim it.

Grok Build also creates a real hazard for **existing Claude Code users**:
it reads `.claude/settings.json` hooks, so athenaeum's Claude Code hook can
fire under a host it was not written for (§4). That is the most urgent work
in the epic.

---

## 2. What the existing host integration is

Each claim below was read at `origin/develop` 0d63b08c.

- **Transport.** `athenaeum serve` is stdio only.
  `src/athenaeum/_cmd_serve.py:cmd_serve` builds the server with
  `athenaeum.mcp_server.create_server` and calls `server.run()` with no
  transport argument. There is no listener and no authentication.
  `--audience` is an operator-pinned read filter, not an identity
  ([security posture §2](security-posture.md)).
- **Tools.** `create_server` registers 15 tools: `recall`, `read_entity`,
  `enumerate_entities`, `entity_schema`, `remember`, `raise_decision`,
  `resolve_question`, `resolve_merge`, `review_audit_item`, and read-only
  queue and audit views. Several of these write.
- **Per-turn push (Claude Code only).**
  `src/athenaeum/claude_code_adapter.py:main` is the `UserPromptSubmit`
  hook. It reads stdin once, appends a witness line
  (`_witness_turn`, which delegates to
  `promptwitness.harness.claude_code.record_turn`), parses `prompt` and
  `session_id` (`_read_hook_input`), and calls
  `athenaeum.context.build_context_for_turn`. That call records push
  telemetry through `record_context_push` and appends to a per-session
  seen-file. The adapter then prints `hookSpecificOutput` for Claude Code to
  inject.
- **Host-neutral core.** `athenaeum.context.build_context` returns a
  versioned envelope (`src/athenaeum/context_schema.py`). The
  [sidecar adapter contract](../extending/sidecar-adapter-contract.md)
  defines what a host adapter may do with that envelope. Its tier taxonomy
  (§3) is the vocabulary this page uses.
- **Tier 2 precedent.** `athenaeum codex-context` writes a periodically
  refreshed snapshot file and relies on a user-added instruction pointing
  the agent at it ([Codex guide](../guides/codex.md)). It is labelled "a
  proof against the contract, not a supported integration".

**Reusable for Grok:** the stdio server unchanged, the `build_context`
core, and the `codex-context` Tier 2 pattern.

**Host-specific and not reusable:** `claude_code_adapter` (Claude Code's
stdin field names and `hookSpecificOutput`), and the witness harness name
`claude-code`.

---

## 3. Grok capabilities: verified and inferred

Legend:

- **V**: stated on an xAI primary source (docs.x.ai, x.ai), on the
  research date.
- **R**: reported by a third party, or by a contributor on an xAI-owned
  repository, but not stated by xAI.
- **I**: inferred by this design and not observed.

### 3.1 Grok apps: custom connectors

- **V.** Users add a server at grok.com/connectors via New Connector, then
  Custom, by entering the URL and completing "any required authentication".
  Grok then discovers the tools.
  <https://docs.x.ai/grok/connectors>
- **V.** The server must be reachable on the public internet. Grok rejects
  localhost and private addresses. xAI's tunnelling guide points at ngrok or
  Cloudflare Tunnel and says "the tunnel only handles network reachability".
  <https://docs.x.ai/grok/connectors/custom-mcp-tunneling>
- **V.** Cloudflare quick tunnels do not support SSE. Streamable HTTP works
  through them. Most free tunnel URLs change on restart, which forces the
  connector to be removed and re-added. (same page)
- **V.** "Bring Your Own MCP" was announced on 2026-05-06. Connectors are on
  web, iOS and Android, and several first-party connectors write.
  <https://x.ai/news/grok-connectors>
- **V.** On Grok Business and Enterprise, an admin provisions the connector
  (Add Connector, then Other, then a URL) before members connect their own
  accounts. <https://docs.x.ai/grok/connector-management>
- **V (absent).** The connector docs name **no transport, no auth method,
  no plan requirement, no tool limit, and no approval control** for custom
  servers.
- **R.** The connector speaks OAuth 2.1 with PKCE (S256) using xAI's
  published client metadata. Discovery runs **only at connect time and only
  if `initialize` itself is challenged**. A later 401 is not a step-up
  trigger, and there is no re-authenticate action. The same behaviour was
  seen on iOS. This comes from an open issue on xAI's own repository and a
  contributor's explanation, not from xAI staff.
  <https://github.com/xai-org/plugin-marketplace/issues/1694>
- **R (conflicting).** Some vendor guides say a static bearer token is
  accepted in the connector form; others say only OAuth works. Treat bearer
  support on the Grok apps as unknown.
- **R (conflicting).** Third-party guides disagree on whether custom
  connectors need a paid plan. xAI's docs say "connectors are available to
  all Grok users" without separating custom servers out.
- **R.** Per-user personalisation exists in some form ("Customize Grok",
  custom instructions, Workspaces, Skills). No xAI primary page describing
  it was found on the research date.
- **I.** No per-prompt hook or extension point exists in the Grok apps. No
  document describes one, and connectors are a pull surface.

### 3.2 Grok Build (terminal agent)

- **V.** Local stdio MCP servers (`command`, `args`, `env`) and Streamable
  HTTP servers are both supported, configured in `config.toml` under
  `[mcp_servers.<name>]`. Grok Build **also loads Claude Code's and Cursor's
  MCP config** (`.claude.json`, project `.mcp.json`, `.cursor/mcp.json`),
  merged below its own config. OAuth is handled for remote servers, and
  static headers are supported.
  <https://docs.x.ai/build/features/mcp-servers>
- **V.** `[compat.claude]` and `[compat.cursor]` each take `skills`, `rules`,
  `agents`, `mcps` and `hooks`. All default to `true` ("Scan Cursor or Claude
  harness directories"), so `[compat.claude] hooks = false` and
  `mcps = false` are the documented user-side opt-outs. The environment
  variable `GROK_CLAUDE_AGENTS_ENABLED` is described as "Scan `CLAUDE.md` /
  `CLAUDE.local.md`". <https://docs.x.ai/build/settings/reference>
  (verified 2026-11-11)
- **V.** The hook events include `SessionStart` and `UserPromptSubmit`. Hook
  stdin carries `hookEventName`, `sessionId`, `cwd` and `workspaceRoot`, and
  each hook process gets `GROK_HOOK_EVENT`, `GROK_HOOK_NAME`,
  `GROK_SESSION_ID` and `GROK_WORKSPACE_ROOT`. "For passive events, stdout
  is ignored." `PreToolUse` is "the only blocking event". Hooks fail open,
  with a default timeout of 5 seconds. Grok Build **reads Claude Code's
  `.claude/settings.json` hooks** and Cursor's `.cursor/hooks.json`. Project
  hooks need trust first (`/hooks-trust`).
  <https://docs.x.ai/build/features/hooks>
- **V (absent).** The hooks page documents no `prompt` field for
  `UserPromptSubmit` and no context-injection output. It does not define
  "passive", and it does not say whether the user-level
  `.claude/settings.json` is read or only the project one.
- **V.** Grok Build authenticates through a browser on first launch, or
  with `XAI_API_KEY`. Headless mode is `grok -p`. `grok inspect` lists the
  discovered "instructions", hooks and MCP servers.
  <https://docs.x.ai/build/overview>
- **I.** **Per-turn push is not available.** If `UserPromptSubmit` is
  passive, its stdout is discarded and nothing reaches the model. This is
  the documented reading. It still needs an `~operator` probe before it is
  treated as settled, because "passive" is undefined.
- **I.** Grok Build reads instruction files: `grok inspect` reports
  "instructions", and `GROK_CLAUDE_AGENTS_ENABLED` implies `CLAUDE.md` is
  scanned. Whether `AGENTS.md` or a Grok-native file is read is
  undocumented. Whether the agent follows a pointer to a context file is
  unobserved. A Tier 2 snapshot is workable only once both are known.

### 3.3 xAI API: remote MCP tools

- **V.** Supported in the xAI native SDK, the OpenAI-compatible Responses
  API and the speech-to-speech API. The fields are `server_url`,
  `server_label`, `server_description`, `allowed_tools` (empty means all),
  `authorization` (a token sent as the `Authorization` header) and
  `headers`. The transport is Streamable HTTP or SSE. xAI's platform runs
  the MCP connection and the tool calls server-side. `require_approval` and
  `connector_id` are not supported in the Responses API.
  <https://docs.x.ai/developers/tools/remote-mcp>
- **I.** A static personal token is enough on this path, because the caller
  supplies `authorization` per request.

---

## 4. The cross-host hook hazard (Grok Build reading Claude Code hooks)

A user who has wired athenaeum's Claude Code hook and then runs Grok Build
in the same project may have `athenaeum-claude-hook` invoked by Grok. What
then happens follows from the code:

1. `claude_code_adapter.main` calls `_witness_turn(cache_dir, raw)`
   **before** any gate. If the witness ledger is enabled, a Grok turn is
   recorded through `promptwitness.harness.claude_code.record_turn`, under
   the `claude-code` harness ledger. Whether `record_turn` accepts the
   camelCase payload is unverified.
2. `_read_hook_input` reads `prompt` and `session_id`. Grok's documented
   payload has neither (`sessionId` is camelCase). So:
   - **if Grok sends no prompt** (as documented), `len(prompt) < 8` returns
     early. Recall is harmless, but the witness line from step 1 has
     already been written.
   - **if Grok does send `prompt`** (undocumented), `build_context_for_turn`
     runs under `session_id = "unknown"`. It records a push the model never
     sees, because stdout is ignored, which **inflates push-precision
     denominators**. It also appends to the one shared `unknown` seen-file,
     which then suppresses those pages across every unknown-session turn.
3. A recall that runs costs up to the foreground hook budget against Grok's
   5-second default timeout. Hooks fail open, so the cost is latency, not a
   block.

**Design rule:** a host adapter must refuse to run under a host it was not
written for, rather than trusting that host to ignore its output. For
`claude_code_adapter`, the cheap and documented discriminator is the
`GROK_HOOK_EVENT` environment variable, which Grok sets for every hook
process. A more general form is to require Claude Code's own payload shape
(`hook_event_name`) and no-op otherwise. Choosing between them belongs to
the implementing issue. Either way, the guard must run **before**
`_witness_turn`.

---

## 5. Architecture

### 5.1 Boundaries

```text
Grok Build (local)                Grok apps / xAI API (remote)
──────────────────                ─────────────────────────────
grok ── stdio ──► athenaeum serve  Grok cloud ── HTTPS ──► [athenaeum#2199
   │                (unchanged)                             remote mode]
   │                                                         │
   └─ instruction file ◄── snapshot adapter                  └─► same tool
        (Tier 2)            (codex-context pattern)              surface,
                                                                 auth-bound
                                                                 audience
```

- **No new server code in this epic.** Grok Build uses `athenaeum serve` as
  it is. The Grok apps use whatever athenaeum#2199 ships. Grok-specific code
  is limited to:
  - the hook guard (§4);
  - at most, a host-parameterised Tier 2 snapshot writer.
- **Repo split.** Everything belongs in this repo as mechanism. A user's
  connector URL, tunnel, token and plan are deployment state and never
  appear in tracked files.
- **No Grok-specific adapter in the core.** If a Tier 2 writer is added, it
  generalises `codex-context` (output path plus header text) rather than
  forking it, per contract §2.2 (no re-rank, no re-query).

### 5.2 What "auto-recall" can honestly mean on Grok

| Mechanism | Tier | Where | What to call it |
|---|---|---|---|
| Per-turn hook push | 1 | Nowhere documented | Not available |
| Periodic snapshot plus instruction pointer | 2 (instruction-mediated) | Grok Build | "Context snapshot", with staleness bounded by the interval |
| Custom instructions or a Skill telling the model to call `recall` first | 3, instructed | Grok apps, Grok Build | "Instructed recall". It depends on model compliance and must be measured, not assumed |
| Caller injects `build_context` output into the system prompt | 1, owned by the caller | xAI API apps | Out of scope (§7) |

Docs and the client table must use these names. "Auto-recall" stays
reserved for Tier 1, consistent with the contract's reason for naming Tier 3
out of scope: a pull surface must never be presented as having replaced
push.

### 5.3 Security model

- **Grok Build.** The same trust boundary as Claude Code: a local process
  that the user's own agent launches. Two Grok-specific points apply:
  - Grok Build auto-loads Claude Code's MCP config, so athenaeum becomes
    available to a second model vendor **without the user opting in**.
    Docs must say so, and name `[compat.claude] mcps = false` (and
    `hooks = false`) as the opt-outs.
  - Prompts and tool results go to xAI rather than Anthropic. That is a
    data-residency fact for the operator, not a defect.
- **Grok apps and xAI API.** Every requirement in athenaeum#2199's security
  notes applies, notably: no unauthenticated tunnel, and audience bound to
  the authenticated identity. On top of those:
  - Tool calls originate from **xAI's cloud**, so source-IP allowlisting
    cannot identify the user.
  - The API cannot require approval (`require_approval` is unsupported), and
    the app docs describe no approval control. **Write-tool gating must
    therefore be enforced server-side.** Default the Grok-facing tool set to
    read-only (`recall`, `read_entity`, `entity_schema`,
    `enumerate_entities`) until the operator opts in to writes.
  - `recall(with_pii=True)` and `read_entity` expose excluded fields
    ([security posture §2](security-posture.md)). They must follow the
    authenticated audience and must never default open to a hosted
    connector.

---

## 6. Requirements Grok places on remote mode (input to athenaeum#2199)

The remote-mode children of athenaeum#2199 that this list feeds:

- athenaeum#2382: transport;
- athenaeum#2384: OAuth and identity-bound audience;
- athenaeum#2383: tool allowlist and the `with_pii` pin.

1. **Challenge `initialize`.** Grok apps discover OAuth only when the
   initial handshake returns 401 (R). A design that allows anonymous
   `initialize` and challenges per tool will appear "connected" in Grok with
   no token, and every call will then run anonymously.
2. **OAuth 2.1 with PKCE (S256), with metadata discovery** for the Grok apps
   (R). A **static bearer token** is enough for the xAI API (V).
3. **Streamable HTTP is the primary transport.** SSE is optional; Cloudflare
   quick tunnels cannot carry it (V).
4. **Server-side tool allowlisting,** because neither Grok surface offers
   client-side approval (V for the API, I for the apps).
5. **Stable hostname guidance,** because a changed tunnel URL forces the
   connector to be re-added (V).
6. **Team mode maps onto Grok Business and Enterprise:** admin provisioning
   plus per-user connect (V). That favours per-user identity and per-user
   audience over one shared token.

---

## 7. Rejected alternatives

1. **Park the whole epic.** Rejected. Grok Build is a working stdio host
   today, and it already loads athenaeum through Claude Code config whether
   or not the project acts. Parking would leave the hook hazard (§4)
   unowned.
2. **A Grok Build Tier 1 adapter that mirrors `claude_code_adapter`.**
   Rejected for now. The documented hook output is discarded, so it would
   record pushes that no model ever sees. Revisit only if the `~operator`
   probe shows `UserPromptSubmit` stdout reaching the model.
3. **A caller-side xAI API adapter** that runs `build_context()` locally and
   puts the rendered text into `instructions` or the system message before
   each request, with tools executed client-side, so no remote mode is
   needed. Technically sound, and it is a genuine Tier 1. Deferred as out of
   scope: "supporting xAI API clients in general" is a stated non-goal of
   athenaeum#2201, and no concrete caller exists. If one appears, it is an
   example under `examples/`, not a core module.
4. **A stdio-to-HTTP proxy plus a public tunnel** so the Grok apps can
   connect now. Rejected for the reasons in athenaeum#2199's security notes:
   it exposes the whole store without authentication.

---

## 8. Failure classes the design must survive

- **xAI changes hook semantics.** The hook guard keys on a documented
  environment variable and fails closed (it does nothing). A new Grok
  injection feature is picked up by the probe, not by guesswork.
- **Instruction non-compliance.** Instructed recall is measured against the
  Claude Code hook before any doc calls it adequate.
- **Snapshot staleness.** This inherits `codex-context`'s
  `stale_after` header and its refresh-failed tombstone.
- **Silent anonymous connection** (Grok marks a connector as connected
  without a token). This is prevented by requirement §6.1.
- **Rollback.** Every Grok-specific change is additive: a guard, a doc, or
  an optional snapshot job. Each one reverts with a single commit and leaves
  Claude Code behaviour untouched.

---

## 9. Open operator decisions

1. **Order of work.** Do Grok Build now, without remote mode, and keep the
   Grok apps gated on athenaeum#2199? *Recommended: yes.*
2. **Vocabulary.** Reserve "auto-recall" for Tier 1, and use "context
   snapshot" and "instructed recall" elsewhere, across every host and not
   only Grok? *Recommended: yes.* The ChatGPT epic athenaeum#2200 faces the
   same question.
3. **Tier 2 writer shape.** Either generalise `codex-context` into one
   host-parameterised `context-file` command, or document running
   `codex-context --output` for Grok. *Recommended: generalise, but only
   after the probe names Grok's instruction file.*
4. **Remote tool surface for Grok.** Read-only by default, with writes as an
   explicit opt-in? *Recommended: read-only default.*
5. **Re-check cadence.** Re-verify §3 against xAI docs on or after
   **2027-02-10**, or sooner if xAI announces hook context injection.
