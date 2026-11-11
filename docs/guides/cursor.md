**Reference:** [mcp](../modules/mcp.md) · [recall](../modules/recall.md)

# Cursor

Athenaeum's MCP server (`athenaeum serve`) speaks MCP over **stdio only** —
the same stdio-only, no-auth posture [Other clients and
platforms](clients.md) describes for every local MCP client. This guide
covers the Cursor-specific setup, what you get, how Cursor's own hook
passthrough interacts with Claude Code's hooks, keeping the search index
current, and what stays unsupported.

## Setup

Add an `mcpServers` entry to Cursor's MCP config — `~/.cursor/mcp.json` for a
server available in every project, or `.cursor/mcp.json` inside a project for
one scoped to that project
([Model Context Protocol](https://cursor.com/docs/context/mcp)):

```json
{
  "mcpServers": {
    "athenaeum": {
      "type": "stdio",
      "command": "/path/to/venv/bin/athenaeum",
      "args": ["serve", "--path", "/absolute/path/to/knowledge"]
    }
  }
}
```

Install the package with the MCP extra first — see
[Install](install.md#i-want-agents-to-write-and-read-memory-through-mcp) for
the command — then resolve the absolute path for `command` with
`which athenaeum`; GUI-launched apps, Cursor included, do not reliably
inherit your shell's `PATH`.

Cursor interpolates `${userHome}` inside `mcp.json` paths
([Model Context Protocol](https://cursor.com/docs/context/mcp)), so `args`
can read `["serve", "--path", "${userHome}/knowledge"]` instead of a literal
absolute path if you would rather not hardcode one.

To pin the server to a restricted read scope instead of full owner access,
add `--audience` to `args` (for example `"--audience", "ops"`) — the same
flag `athenaeum serve` and `recall` take everywhere else; comma-separated
role/group ids, unset meaning owner with full access. See the
[CLI reference](../reference/cli.md#athenaeum-serve) for the full
description.

The Cursor CLI (the `agent` command) reads the same `mcp.json` configuration
as the Cursor editor, at both the global and project scope
([CLI: Model Context Protocol](https://cursor.com/docs/cli/mcp)) — no
separate setup is needed to also run athenaeum under the CLI.

Restart Cursor (or the CLI session) after editing `mcp.json`.

## What you get

Every one of athenaeum's MCP tools — `recall`, `remember`, the
decision-queue tools, all of them — is available to Cursor **by explicit
tool call**: Cursor decides when to call a tool, the same as any other local
(stdio) MCP client (see [Other clients and platforms](clients.md)). See
[the MCP surface](../modules/mcp.md) for the full tool list.

**Per-turn auto-recall is not available on Cursor as documented.** Claude
Code's passive, every-turn context injection (see [Passive recall via
hooks](sidecar.md)) is implemented as a Claude Code `UserPromptSubmit` hook;
Cursor has no equivalent per-turn injection point of its own, and the hook
passthrough described below is an explicit no-op for athenaeum today. The
closest thing Cursor supports is an always-applied rule that instructs the
agent to call `recall` itself — see the next section.

## Instructed recall (rule)

A Cursor rule with `alwaysApply: true` is applied to every chat session,
including cloud agents, so it can carry an explicit instruction to call
`recall` and `remember` where Cursor has no auto-recall hook of its own
([Rules](https://cursor.com/docs/context/rules)). An example rule ships at
`examples/cursor/athenaeum.mdc` — see its
[README](../../examples/cursor/README.md) for how to install it as a
project or user rule.

This is **instructed recall, not auto-recall**: Cursor guarantees the rule
is loaded into every chat, but whether the agent actually calls `recall`
before answering, or `remember` when the user states a durable fact, still
depends on the model following the instruction. Treat it as a tier with an
instruction, not a guarantee, until it has been measured.

## Claude Code hooks under Cursor

Cursor runs `~/.claude/settings.json` hooks by default — it maps its own
`beforeSubmitPrompt` event onto Claude Code's `UserPromptSubmit` hook through
its "Include Third-Party Plugins, Skills, and Other Configs" setting
(Settings → Agents → Third-Party Imports), which is **on** by default
([Third-Party Hooks](https://cursor.com/docs/reference/third-party-hooks)).
If you have athenaeum's hook wired for Claude Code, Cursor will invoke it.

Athenaeum's hook adapter detects a Cursor-origin invocation and **no-ops**:
no recall, no output, exit 0 — the same guard that makes the adapter no-op
under Grok Build extends to Cursor. See [Passive recall via
hooks](sidecar.md), which carries the same note. To stop Cursor from
invoking Claude Code hooks at all, turn off "Include Third-Party Plugins,
Skills, and Other Configs" — that is the user-side opt-out
([Third-Party Hooks](https://cursor.com/docs/reference/third-party-hooks)).

## Search index

`recall` reads an FTS5 (or vector) index. Under Claude Code, that index is
built by the SessionStart hook; Cursor has no equivalent hook that builds it
for you. Run `athenaeum reindex` after each compile, or `athenaeum
session-end` (which compiles and reindexes in one change-gated step) — see
[Daily operation](daily-operation.md) for both. Skip this and `recall` under
Cursor will not see anything compiled since the last index build.

## Egress and platform

Prompts, tool calls, and anything `recall` returns go to whatever model
provider Cursor is configured to route that session to — the same boundary
that applies to any MCP client talking to a model provider other than the
one athenaeum's own compile step uses.

The hooks Cursor would run under the passthrough above are the same bash
scripts documented in [Other clients and platforms](clients.md#which-operating-systems-are-supported)
— unsupported on Windows for the same reason: no Windows CI, untested file
locking.

Cursor's cloud agents (web, mobile, Slack) run on Cursor's own
infrastructure rather than your machine and cannot launch a local process,
so they cannot reach a local stdio MCP server at all. Only the desktop
editor and the CLI — both of which launch `athenaeum serve` as a local
subprocess — are supported.

## See also

- Guides — [Other clients and platforms](clients.md) · [Install](install.md) · [Passive recall via hooks](sidecar.md) · [Daily operation](daily-operation.md)
- Modules — [mcp](../modules/mcp.md) · [recall](../modules/recall.md)
