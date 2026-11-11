<!-- SPDX-License-Identifier: Apache-2.0 -->

# Cursor as an athenaeum host

**Status:** PROPOSED (Vitruvius Specify, 2026-11-11). Scopes the epic
athenaeum#2202. The shared remote (HTTPS) prerequisite is owned by
athenaeum#2199 and is not designed here; this page states only what Cursor
needs from it (§6).

Companion notes: [Grok as a host](host-grok.md) and
[ChatGPT as a host](host-chatgpt.md). This page uses the same vocabulary: the
three-tier host taxonomy of the
[sidecar adapter contract §3](../extending/sidecar-adapter-contract.md#3-the-three-tier-host-taxonomy),
and the operator's wording rule that **"auto-recall" means per-turn context
push only**. Anything else is called a "context snapshot", "instructed
recall" or "steered recall".

Research date for every external claim: **2026-11-11**. Cursor ships weekly;
re-check §3 before building on a claim older than about three months (§9).

---

## 1. Summary

| Surface | How it reaches MCP | Needs athenaeum#2199? | Best recall available |
|---|---|---|---|
| **Cursor desktop** (the editor's agent) | Launches **local stdio** servers from `.cursor/mcp.json` or `~/.cursor/mcp.json` | **No** | Session-start context snapshot (`sessionStart` hook) plus instructed recall (a rule). Per-turn push is **not available as documented** (§3.3). |
| **Cursor CLI** (`agent`) | Same MCP config as the editor | **No** | Same as desktop, subject to the probe (§3.6) |
| **Cloud agents** (web, iOS, Slack, GitHub, Linear, API) | MCP inside a cloud VM; cannot reach the user's machine | **Yes** | Explicit tools only. `sessionStart` and user-level hooks do not run there. Non-goal for this epic (§5.4). |

**Verdict.**

- Cursor desktop and the CLI are working stdio hosts today. They need nothing
  from remote mode.
- **True auto-recall is not possible on Cursor as documented.** Cursor's
  per-prompt hook, `beforeSubmitPrompt`, can allow or block a prompt but
  cannot add context to it. The only documented injection points are
  `sessionStart` (once per conversation, no prompt) and `postToolUse` (after
  a tool call). An `~operator` probe must still check one undocumented path
  before this is final (§3.4, item 3).
- What Cursor can honestly get: every MCP tool by explicit call, a **context
  snapshot** injected by the host at session start, and **instructed recall**
  through an always-applied rule.

**Hazard found, and it is worse than Grok's.** Cursor runs Claude Code's hooks
from `~/.claude/settings.json` **by default**, and maps `UserPromptSubmit` to
`beforeSubmitPrompt`, whose payload carries `prompt` but no `session_id`. So
every existing athenaeum hook user who opens Cursor records a push under
`session_id="unknown"` that no model ever sees (§4). That is the most urgent
work in the epic.

---

## 2. What the existing host integration is

Each claim below was read at `origin/develop` 0d63b08c.

- **Transport.** `src/athenaeum/_cmd_serve.py:cmd_serve` builds the server
  with `athenaeum.mcp_server.create_server` and calls `server.run()` with no
  transport argument: stdio only, no listener, no authentication. `--audience`
  is an operator-pinned read filter, not an identity
  ([security posture §2](security-posture.md)).
- **Per-turn push (Claude Code only).**
  `src/athenaeum/claude_code_adapter.py:main` is the `UserPromptSubmit` hook,
  installed as the `athenaeum-claude-hook` console script. In order, it:
  1. reads stdin once;
  2. calls `_witness_turn(cache_dir, raw)` **before any gate**, which delegates
     to `promptwitness.harness.claude_code.record_turn` when the witness ledger
     is enabled;
  3. parses the payload with `_read_hook_input`, which reads `prompt` and
     `session_id` and falls back to `"unknown"` when `session_id` is absent;
  4. returns early if `AUTO_RECALL` is off or the prompt is shorter than 8
     characters;
  5. calls `athenaeum.context.build_context_for_turn`, which records push
     telemetry and appends to a per-session seen-file;
  6. prints `hookSpecificOutput.additionalContext` (`_hook_output`).
- **The shell launcher.** `examples/claude-code/user-prompt-recall.sh` is a
  thin launcher that `exec`s the same adapter
  ([sidecar guide](../guides/sidecar.md)). One guard in the adapter covers
  both install shapes.
- **Other kit hooks** (`examples/claude-code/settings-snippet.json`):
  `session-start-recall.sh` (index build, 60-second timeout),
  `wiki-context-inject.sh` (plain-stdout cwd keyword match),
  `pending-questions-surface.sh`, `pre-compact-save.sh`, and a `SessionEnd`
  `rebuild-index.sh`. `athenaeum session-end` calls
  `claude_code_adapter.witness_session_end`.
- **Tier 2 precedent.** `src/athenaeum/codex_adapter.py` (`athenaeum
  codex-context`) writes a periodic snapshot with one `build_context` call,
  `use_llm=False`, an empty exclude set and no seen-file, and records no push
  telemetry (its finding F1). It is labelled "a proof against the contract,
  not a supported integration" ([Codex guide](../guides/codex.md)).
- **Current docs.** [`clients.md`](../guides/clients.md) lists Cursor as
  "Works: all MCP tools, by explicit tool call only". Its example server entry
  has no `type` field.

**Reusable for Cursor:** the stdio server unchanged; the `build_context` core;
the `codex-context` snapshot discipline (empty exclude, no seen-file).

**Host-specific and not reusable:** `claude_code_adapter` (Claude Code's stdin
field names, `hookSpecificOutput`, the `claude-code` witness harness).

---

## 3. Cursor capabilities: verified and inferred

Legend:

- **V**: stated on a Cursor primary source (cursor.com docs, changelog or
  release notes) on the research date.
- **R**: reported by a third party, including Cursor's community forum, but
  not stated in Cursor's docs.
- **I**: inferred by this design and not observed.

### 3.1 Local stdio MCP

- **V.** Cursor supports three transports: stdio, SSE and Streamable HTTP.
  Project config is `.cursor/mcp.json`; global config is `~/.cursor/mcp.json`.
  The top-level key is `mcpServers`. A stdio entry takes `type` (`"stdio"`),
  `command`, `args`, `env` and `envFile`.
  <https://cursor.com/docs/context/mcp>
- **V.** Cursor interpolates `${env:NAME}`, `${userHome}`,
  `${workspaceFolder}`, `${workspaceFolderBasename}` and `${pathSeparator}` in
  `command`, `args`, `env`, `url` and `headers`. (same page)
- **V.** The protocol support table lists Tools, Prompts, Resources, Roots,
  Elicitation and Apps. (same page)
- **I.** Server `instructions` and tool annotations are not listed, so they
  are assumed unsupported. Steering through them (athenaeum#2387's mechanism)
  is not counted on for Cursor.
- **I.** The historical 40-tool cap is no longer documented. athenaeum
  registers 15 tools, so it is not a constraint either way.
- **I.** Whether `type: "stdio"` is required, or inferred from `command` as
  the current `clients.md` example assumes, is unverified (probe, §3.6).
- **I.** Cursor does not read Claude Code's MCP registration (`~/.claude.json`
  or `.mcp.json`); no Cursor page mentions it. A user needs a Cursor entry of
  their own.

### 3.2 Remote MCP and OAuth

- **V.** Remote servers use `url` and `headers` over SSE or Streamable HTTP.
  OAuth uses the authorization code flow with PKCE. Cursor supports Dynamic
  Client Registration ("registers as a public client with no client secret")
  and, when the provider has no DCR, a static `auth` object with a client ID
  and optional secret and scopes. The redirect URLs are fixed:
  `https://www.cursor.com/agents/mcp/oauth/callback` (web) and
  `http://localhost:8787/callback` (desktop).
  <https://cursor.com/docs/context/mcp>
- **I.** Client ID Metadata Documents (CIMD) are not mentioned, so a remote
  athenaeum that wants Cursor must work with an authorization server that
  offers DCR or a pre-registered client.

### 3.3 Hooks: the auto-recall question

- **V.** Hooks were introduced in Cursor 1.7 (2025-09-29), as beta.
  <https://cursor.com/changelog/1-7>
- **V.** Agent events include `sessionStart`, `sessionEnd`, `preToolUse`,
  `postToolUse`, `postToolUseFailure`, `beforeShellExecution`,
  `beforeMCPExecution`, `afterMCPExecution`, `beforeReadFile`,
  `afterFileEdit`, **`beforeSubmitPrompt`**, `preCompact`, `stop`,
  `afterAgentResponse` and `afterAgentThought`. Config files, highest
  priority first: enterprise, team (dashboard), project
  `.cursor/hooks.json`, user `~/.cursor/hooks.json`.
  <https://cursor.com/docs/hooks>
- **V.** Every hook receives `conversation_id`, `generation_id`, `model`,
  `hook_event_name`, `cursor_version`, `workspace_roots`, `user_email` and
  `transcript_path`. Hook processes get `CURSOR_PROJECT_DIR`,
  `CURSOR_VERSION`, `CURSOR_USER_EMAIL`, `CURSOR_TRANSCRIPT_PATH` and
  `CLAUDE_PROJECT_DIR` ("Alias for project dir (Claude compatibility)").
  (same page)
- **V. `beforeSubmitPrompt` cannot inject context.** Input: `prompt` and
  `attachments[]`. Output: only `continue` ("Whether to allow the prompt
  submission to proceed") and `user_message` ("Message shown to the user when
  the prompt is blocked"). (same page)
- **V. `sessionStart` can inject context, once.** Output `additional_context`:
  "Additional context to add to the conversation's initial system context",
  plus `env` for later hooks. "This hook runs as fire-and-forget; the agent
  loop does not wait for or enforce a blocking response." Its input adds
  `session_id`, `is_background_agent` and `composer_mode`. (same page)
- **V.** `postToolUse` and `postToolUseFailure` return `additional_context`,
  "injected into the conversation after the tool result". `stop` returns
  `followup_message`, which continues the agent loop. (same page)
- **V.** The per-script `timeout` default is given only as "platform
  default". Windows is supported: commands run through PowerShell. (same
  page)
- **I.** No changelog or release-note entry adds injection to
  `beforeSubmitPrompt`. **Per-turn push is therefore not available as
  documented.** One undocumented path remains (§3.4).
- **I.** Because `sessionStart` is fire-and-forget, a slow hook may lose the
  race with the first turn. Whether its `additional_context` reliably lands
  is unobserved (probe).

### 3.4 Cursor runs Claude Code's configuration

- **V.** Cursor loads Claude Code hooks from `.claude/settings.local.json`,
  `.claude/settings.json` and **`~/.claude/settings.json`** when "Include
  Third-Party Plugins, Skills, and Other Configs" is enabled, and "The setting
  is on by default." `UserPromptSubmit` maps to `beforeSubmitPrompt`;
  `SessionStart`, `SessionEnd`, `PreCompact`, `Stop` and the tool events map
  to their Cursor equivalents. "Cursor supports both Claude Code's nested
  `hookSpecificOutput` response format and the older flat response format."
  <https://cursor.com/docs/reference/third-party-hooks>
- **V.** The CLI reads and merges Claude Code `settings.json` hooks
  (January 2026) and accepts Claude Code-format hook responses (April 2026).
  <https://cursor.com/docs/cli/changelog>
- **V.** Cursor loads skills from `.claude/skills/` and `~/.claude/skills/`
  (<https://cursor.com/docs/skills>), and reads `CLAUDE.md` (release 3.26
  de-duplicates it against `AGENTS.md`; <https://cursor.com/docs/release-notes>).
- **V (absent).** The third-party page does not document the stdin payload a
  Claude-format hook receives, and says nothing about `additionalContext`.
- **I.** The payload is Cursor's own schema: `prompt` and `conversation_id`,
  `hook_event_name`, `cursor_version`, and **no `session_id`** on
  `beforeSubmitPrompt`.
- **I, open.** Whether a Claude-format `UserPromptSubmit` response's
  `hookSpecificOutput.additionalContext` reaches the model. Cursor accepts the
  nested format, but its native `beforeSubmitPrompt` has no field to map it
  to. **If it does reach the model, Cursor has real auto-recall through the
  existing adapter.** That is the single most valuable fact for the probe to
  settle.
- **R.** A forum report against Cursor 2.6.11 says `.claude/settings.json`
  hooks never fired. <https://forum.cursor.com/t/claude-hooks-dont-work/153614>

### 3.5 Rules and AGENTS.md (the instruction channel)

- **V.** Project rules live in `.cursor/rules`, with frontmatter
  `description`, `globs` and `alwaysApply`. "If alwaysApply is true, the rule
  will be applied to every chat session." Team, project and user rules exist.
  Cursor supports `AGENTS.md` in the project root and subdirectories.
  <https://cursor.com/docs/context/rules>
- **V.** Rule contents "are included at the start of the model context".
  (same page)
- **I.** Rules are added once per conversation, not re-read each turn
  (release v2026.10.22 says nested rules show "once per conversation";
  <https://cursor.com/docs/release-notes>). A snapshot file rewritten
  mid-conversation is not seen until a new chat. This favours `sessionStart`
  injection over a rule-referenced file for the snapshot, and makes a rule
  the natural home for an instruction, not for data.

### 3.6 Cloud agents, CLI and plans

- **V.** Cloud agents run "in isolated VMs in the cloud" and support MCP over
  both HTTP and stdio. They run command hooks from project
  `.cursor/hooks.json`, but **not** `sessionStart`, `sessionEnd`, MCP hooks,
  or user-level `~/.cursor/hooks.json`. Surfaces include web, iOS, Slack,
  GitHub, Linear and an API. <https://cursor.com/docs/cloud-agent>
- **V.** Cloud agents keep their own MCP sign-ins on Cursor's servers.
  <https://cursor.com/docs/context/mcp>
- **I.** A cloud agent cannot reach a stdio server on the user's machine; a
  stdio server inside the VM would have no store.
- **V.** The CLI uses "the same configuration as the editor" for MCP
  (<https://cursor.com/docs/cli/mcp>) and runs session start, end and stop
  hooks (<https://cursor.com/docs/cli/changelog>).
- **V.** Cloud agents need a paid plan ("Free and Plus don't include them").
  <https://cursor.com/docs/cloud-agent>
- **I.** MCP, hooks and rules appear on every plan. The pricing page read on
  the research date did not match Cursor's usual plan names and should be
  re-checked by hand before any doc cites it. Enterprise admins can allowlist
  MCP servers by command pattern, URL pattern and tool.
  <https://cursor.com/docs/context/mcp>
- **R.** Cursor's built-in Memories UI was removed in 2.1. There is no overlap
  to manage. <https://forum.cursor.com/t/custom-modes-and-memories-gone-in-2-1/143744>

---

## 4. The cross-host hook hazard (Cursor running Claude Code hooks)

A user who wired athenaeum's Claude Code hooks in `~/.claude/settings.json`
and opens Cursor with the default settings has `athenaeum-claude-hook`
invoked on every Cursor prompt. Following
`claude_code_adapter.main` (§2):

1. `_witness_turn` runs first. With the witness ledger enabled, a Cursor turn
   is recorded under the `claude-code` harness ledger.
2. `_read_hook_input` finds `prompt` (Cursor documents it) and no `session_id`
   (Cursor sends `conversation_id`). The session falls back to `"unknown"`.
3. `build_context_for_turn` runs, records a push through
   `record_context_push`, and appends to the one shared `unknown` seen-file.
   Unless the open question in §3.4 resolves the other way, the model never
   sees that context. **Every Cursor turn is a phantom push** that inflates
   push-precision denominators, and the `unknown` seen-file then suppresses
   those pages on every other unknown-session turn.
4. The recall spends up to the foreground hook budget on each Cursor prompt.

On Grok Build the phantom push needed an undocumented `prompt` field. On
Cursor it is the documented path. The other mapped kit hooks also run:
`session-start-recall.sh` (an index build, harmless but slow),
`wiki-context-inject.sh` (plain stdout, which Cursor's JSON `sessionStart`
contract likely ignores), and `athenaeum session-end` on `SessionEnd`, which
reaches `witness_session_end`.

**Design rule (shared with [host-grok.md §4](host-grok.md)):** a host adapter
must refuse to run under a host it was not written for, before
`_witness_turn`.

**Why athenaeum#2371's general discriminator is not enough.** athenaeum#2371
offers "Claude Code's own payload shape (`hook_event_name` present)" as the
general guard. Cursor's common payload carries `hook_event_name` too, so that
check lets Cursor through. Two discriminators work for Cursor:

- **Explicit Cursor markers.** `CURSOR_VERSION` in the environment, or
  `cursor_version` or `conversation_id` in the payload. All three are
  documented.
- **Positive identification of Claude Code.** No-op unless `session_id` is
  present. This is stricter. It also changes the existing `"unknown"`
  fallback for genuine Claude Code payloads, so it needs a decision.

The fix is a sibling of athenaeum#2371, not an edit to it, and the two should
land as one guard function with one test table.

---

## 5. Architecture

### 5.1 Boundaries

```text
Cursor desktop / CLI (local)
────────────────────────────
cursor ── stdio ──► athenaeum serve            (unchanged)
   │
   ├─ sessionStart hook ──► Cursor adapter ──► build_context()   (snapshot)
   │                         additional_context
   ├─ .cursor/rules/*.mdc ── "call recall when…"             (instructed)
   │
   └─ ~/.claude/settings.json hooks ──► athenaeum-claude-hook ──► no-op guard

Cloud agents (remote) ── HTTPS ──► [athenaeum#2199 remote mode]   (non-goal)
```

- **No new server code.** Cursor uses `athenaeum serve` as it is.
- **Cursor-specific code is limited to:**
  - the hook guard (§4);
  - at most, a small `sessionStart` adapter that calls `build_context` and
    prints `{"additional_context": ...}`;
  - a rule template and a guide.
- **Repo split.** All of it is mechanism and belongs in this repo. A user's
  `mcp.json`, hooks file, rules and plan are deployment state and never appear
  in tracked files.
- **No forked core.** The `sessionStart` adapter follows the `codex-context`
  discipline of contract §3.1: one `build_context` call, an empty exclude
  set, no seen-file, and `render.text` written verbatim. Its seed is the
  workspace root names from the payload, because no prompt exists at session
  start.

### 5.2 What "auto-recall" can honestly mean on Cursor

| Mechanism | Tier | Where | What to call it |
|---|---|---|---|
| Per-turn hook push | 1 | Not documented. Possible only if the probe shows Claude-format `additionalContext` reaching the model | "Auto-recall" only if the probe proves it |
| `sessionStart` `additional_context`, computed fresh per conversation | 2 (host-loaded) | Desktop, CLI | "Context snapshot" (session-start) |
| An `alwaysApply` rule telling the model when to call `recall` | 3, instructed | Desktop, CLI, cloud agents (project rules) | "Instructed recall". Measured, not assumed |
| `recall` by the model's own choice | 3 | Everywhere MCP reaches | Explicit tools |

The epic title and `clients.md` must follow this table. "Including
auto-recall" is not a claim Cursor can carry today.

### 5.3 Security model

- **Same trust boundary as Claude Code.** A local process launched by the
  user's own agent.
- **Data egress.** Prompts, tool results and injected context go to whichever
  model provider Cursor routes to, not necessarily Anthropic. That is a fact
  the guide states, not a defect.
- **Implicit cross-vendor reach.** Cursor runs Claude Code hooks and loads
  Claude Code skills by default. After the guard lands, the hooks do nothing.
  The guide names the "Include Third-Party Plugins, Skills, and Other
  Configs" setting as the user-side opt-out.
- **Audience.** The `sessionStart` adapter inherits `codex-context`'s
  fail-closed egress rule: if any candidate's audience is non-public, the
  whole snapshot is withheld (`codex_adapter.py` module docstring). The stdio
  `--audience` flag applies to `athenaeum serve` exactly as for other stdio
  clients.

### 5.4 Cloud agents are a non-goal

They need remote mode, they skip `sessionStart` and user-level hooks, and they
need a paid plan. Supporting them later is a remote-mode host like ChatGPT,
not a Cursor-desktop problem. §6 records what they would need, as input to
athenaeum#2199.

---

## 6. Cursor and remote mode (athenaeum#2199)

**Cursor desktop and the CLI do not need remote mode.** The adoption order
(athenaeum#2296 → athenaeum#2202 → athenaeum#2199 → athenaeum#2200 →
athenaeum#2201, ruling of 2026-11-05) is sequencing, not a technical
dependency. Today the remote-mode roots athenaeum#2381, athenaeum#2382 and
athenaeum#2383, and the epic athenaeum#2199 itself, are `blocked_by` the
whole Cursor epic. That makes remote mode wait on `~operator` probes and a
live measurement that have nothing to do with it. §9 decision 1 proposes a
lighter edge.

If Cursor cloud agents are ever in scope, they put these requirements on
athenaeum#2382, athenaeum#2383 and athenaeum#2384:

1. **Streamable HTTP or SSE** (V). Streamable HTTP is enough.
2. **OAuth with PKCE, through DCR as a public client, or a pre-registered
   static client ID** (V). CIMD is not documented (I), so an authorization
   server that offers only CIMD would not work for Cursor.
3. **Fixed redirect URIs** (V) must be allowed by the deployment's
   authorization server.
4. **Server-side tool allowlisting.** Tool annotations are not listed as
   supported (I), so the remote profile (athenaeum#2383) is the only gate.

---

## 7. Rejected alternatives

1. **Keep 2202 as "explicit tools only, nothing to do".** Rejected. The hook
   hazard (§4) affects existing Claude Code users today, whether or not the
   project acts on Cursor.
2. **A `beforeSubmitPrompt` adapter that mirrors `claude_code_adapter`.**
   Rejected unless the probe shows injected context reaching the model. The
   documented output is allow or block, so it would record pushes no model
   sees.
3. **Inject on `postToolUse`.** Rejected. It fires after tool calls, not per
   turn. A turn with no tool call gets nothing, and a turn with many calls
   gets repeated context. It is neither auto-recall nor a clean snapshot.
4. **Use `stop.followup_message` to force a recall turn.** Rejected. It
   continues the agent loop after the answer is given: recall arrives too late
   and costs a model turn.
5. **A snapshot file referenced from a rule, refreshed on a timer** (the
   `codex-context` shape). Rejected as the primary path. Rules load once per
   conversation (§3.5), so a refreshed file is not re-read, and Cursor has a
   host-loaded `sessionStart` channel that the Codex host lacks. Kept as a
   fallback if `sessionStart` injection proves unreliable.
6. **Remote mode for Cursor desktop.** Rejected. stdio works.

---

## 8. Failure classes the design must survive

- **Cursor changes hook semantics** (adds prompt injection, or changes the
  Claude-format payload). The guard keys on documented markers and fails
  closed (does nothing). A new injection field is picked up by the re-check,
  not by guessing.
- **`sessionStart` loses the race** (fire-and-forget). The adapter must stay
  inside the foreground hook budget and print nothing on failure. A lost
  injection degrades to explicit tools, never to a blocked session.
- **Instruction non-compliance.** Instructed recall is measured against the
  Claude Code hook baseline before any doc calls it adequate.
- **Windows.** Cursor runs hooks through PowerShell. athenaeum's hooks are
  not supported on Windows (athenaeum#2203); the guide says so.
- **Rollback.** Every Cursor-specific change is additive: a guard, an
  optional console script, a template and a doc. Each reverts in one commit
  and leaves Claude Code behaviour untouched.

---

## 9. Open operator decisions

1. **The 2202 → 2199 edge.** Re-point athenaeum#2381, athenaeum#2382 and
   athenaeum#2383 from the epic athenaeum#2202 to the children that mark
   "Cursor desktop works": the hook guard and the setup guide. Drop the
   epic-to-epic edge on athenaeum#2199. *Recommended: yes.* The adoption order
   still holds, because remote mode starts only after Cursor desktop ships,
   but it no longer waits on Cursor's live probe and measurement.
2. **Hook-guard discriminator.** Explicit Cursor markers, or positive
   identification of Claude Code (`session_id` required). *Recommended:
   explicit markers, plus `GROK_HOOK_EVENT`, in one shared guard with
   athenaeum#2371.* Positive identification is cleaner but changes the
   `"unknown"` fallback for genuine Claude Code payloads, and so needs its own
   evidence that Claude Code always sends `session_id`.
3. **Snapshot adapter shape.** A packaged console script
   (`athenaeum-cursor-hook`, like `athenaeum-claude-hook`), or an example
   under `examples/cursor/`. *Recommended: packaged, but only after the probe
   shows `sessionStart` injection landing.*
4. **Epic title.** Retitle athenaeum#2202 to drop "including auto-recall", as
   athenaeum#2201 was. *Recommended: yes.*
5. **Re-check cadence.** Re-verify §3 on or after **2027-02-11**, or sooner if
   Cursor adds context injection to `beforeSubmitPrompt`.

Decisions 1 to 3 are tracked in athenaeum#2397. Decision 4 was applied under
the 2026-11-11 wording ruling.

---

## 10. Children of athenaeum#2202

| Issue | What | Lifecycle | Blocked by |
|---|---|---|---|
| athenaeum#2394 | Claude Code hook no-ops under Cursor (§4). `moscow:should` | `ready` | athenaeum#2371 |
| athenaeum#2396 | Cursor setup guide and `clients.md` row | `ready` | athenaeum#2394, athenaeum#2372 |
| athenaeum#2397 | Decision: §9 items 1 to 3 | `needs-decision` | — |
| athenaeum#2395 | Probe on a real Cursor install (§3.3, §3.4, §3.5) | `~operator` | — |
| athenaeum#2399 | Instructed recall: example `alwaysApply` rule | `ready` | athenaeum#2396 |
| athenaeum#2398 | `sessionStart` context-snapshot adapter | `~planning` until the probe shows injection landing | athenaeum#2395, athenaeum#2396, athenaeum#2397 |
| athenaeum#2400 | Measure recall against the Claude Code hook baseline | `~operator` | athenaeum#2398, athenaeum#2399 |
