**Reference:** [mcp](../modules/mcp.md) · [recall](../modules/recall.md)

# Other clients and platforms

Athenaeum's MCP server (`athenaeum serve`) speaks MCP over **stdio by default** — no
network listener, no authentication — and over **streamable HTTP** when you opt in and
deploy it yourself behind TLS and an external OIDC provider, which is
[Remote mode](remote.md). Auto-recall (context injected on every turn without a tool
call) exists only through Claude Code's hooks. Those two facts decide what works
where.

| Client | Status | What you get |
|---|---|---|
| Claude Code | Supported | All MCP tools **and** auto-recall via hooks — see [Passive recall via hooks](sidecar.md) |
| Cursor | Works | All MCP tools by explicit call; see [Cursor guide](cursor.md) |
| Claude Desktop, other local (stdio) MCP clients | Works | All MCP tools, by **explicit tool call only** — no auto-recall |
| Grok Build | Works | All MCP tools by explicit call, no auto-recall — see [I want to use Grok Build](#i-want-to-use-grok-build) |
| ChatGPT | Needs remote mode | All MCP tools by explicit call, no auto-recall — once you deploy [Remote mode](remote.md) |
| Grok apps (grok.com, iOS, Android) | Needs remote mode | All MCP tools by explicit call, no auto-recall — once you deploy [Remote mode](remote.md) |
| xAI API | Not supported | — (out of scope) |
| OpenAI Codex | Static snapshot | A periodically refreshed context file — see [Codex context file](codex.md) |

## I want to use Claude Code

Full support. Register the server and wire the hooks: [Install](install.md) and
[Passive recall via hooks](sidecar.md).

## I want to use Claude Desktop, Cursor, or another local MCP client

Any client that can launch a local process over stdio can run `athenaeum serve`.
The model gets every MCP tool (`remember`, `recall`, the decision-queue tools), but
it calls them only when it decides to — there is no per-turn auto-recall outside
Claude Code. **Cursor has its own guide** — see [Cursor](cursor.md) for setup, the
Claude Code hook passthrough, and what stays unsupported. Further Cursor work is
tracked in the
[Cursor support issue](https://github.com/Kromatic-Innovation/athenaeum/issues/2202).

1. Install with the MCP extra — see [Install](install.md#i-want-agents-to-write-and-read-memory-through-mcp)
   for the command.
2. Find the absolute path to the `athenaeum` binary (`which athenaeum`). GUI apps do
   not read your shell's `PATH`, so a bare `athenaeum` usually won't be found.
3. Add a server entry. For **Claude Desktop** on macOS, edit
   `~/Library/Application Support/Claude/claude_desktop_config.json`:

   ```json
   {
     "mcpServers": {
       "athenaeum": {
         "command": "/path/to/venv/bin/athenaeum",
         "args": ["serve", "--path", "/absolute/path/to/knowledge"]
       }
     }
   }
   ```

   Most other stdio clients take the same `mcpServers` shape. **Cursor's config
   additionally takes a `"type": "stdio"` field** (`~/.cursor/mcp.json`, or
   `.cursor/mcp.json` in a project) — see [Cursor](cursor.md) for the full example.
   Use absolute paths for both the binary and `--path`.
4. Restart the client.

`serve` itself needs **no API key** — it only reads and writes files. Two things
still apply:

- **Compiling** (`athenaeum run`, which turns `remember`ed observations into wiki
  pages) needs `ANTHROPIC_API_KEY` in its own environment or `llm.provider:
  claude-cli` — see [Supplying an API key](../../examples/claude-code/README.md#supplying-an-api-key).
- **The search index** that `recall` reads is built by Claude Code's SessionStart
  hook. Without those hooks, run `athenaeum reindex` after each compile (or use
  `athenaeum session-end`, which compiles and reindexes); otherwise `recall` returns
  nothing new. See [Daily operation](daily-operation.md).

## I want to use Grok Build

Grok Build, xAI's terminal agent, launches local MCP servers over stdio the same
way Claude Code and Cursor do — it is not limited to remote HTTPS the way the
other Grok surfaces are. **These facts are unverified on a live install:** they
come from xAI's own documentation (https://docs.x.ai/build/features/mcp-servers),
not a tested athenaeum installation, pending a report from the Grok Build probe.

1. Install with the MCP extra — see [Install](install.md#i-want-agents-to-write-and-read-memory-through-mcp)
   for the command.
2. Find the absolute path to the `athenaeum` binary (`which athenaeum`).
3. Add a server entry to Grok Build's own config:

   ```toml
   [mcp_servers.athenaeum]
   command = "/opt/athenaeum/.venv/bin/athenaeum"
   args = ["serve", "--path", "/opt/athenaeum/knowledge"]
   ```

   Use absolute paths for both the binary and `--path` — adjust them to wherever
   your own virtualenv and knowledge store actually live.

Two things set Grok Build apart from a generic stdio client:

- **It also loads Claude Code's MCP config automatically.** If you already
  registered athenaeum for Claude Code, Grok Build picks it up with no separate
  opt-in step. To opt out, set `[compat.claude] mcps = false` in Grok Build's
  own config.
- **Prompts and tool results go to xAI**, not Anthropic — the same boundary
  that applies to any MCP client talking to a different model provider.

Grok Build gets every MCP tool by **explicit tool call only** — there is no
per-turn auto-recall on Grok Build, the same limitation other local (stdio)
clients have above. For the fuller host analysis (including why no Grok surface
gets Tier-1 auto-recall), see the
[Grok host design note](https://github.com/Kromatic-Innovation/athenaeum/pull/2369)
— landing separately and not yet merged as of this writing.

## I want to use ChatGPT, Grok apps, or the xAI API

ChatGPT, the Grok apps (grok.com, iOS, Android), and the xAI API all connect only to
a **remote MCP server over HTTPS**; none of them can launch a local process. (Grok
Build is different — see [I want to use Grok Build](#i-want-to-use-grok-build)
above.)

For **ChatGPT and the Grok apps**, that server is what remote mode is: `athenaeum
serve --transport http` running as an OAuth 2.1 resource server behind your own TLS
proxy and your own OIDC provider. Read **[Remote mode](remote.md)** before deploying
one — every result a remote host reads is egress to that host's provider, and most of
the guide is about the decisions that follow from it. Host-specific connector setup is
tracked in the
[ChatGPT epic](https://github.com/Kromatic-Innovation/athenaeum/issues/2200) and the
[Grok epic](https://github.com/Kromatic-Innovation/athenaeum/issues/2201).

The **xAI API** stays out of scope, for a different reason: it is a model API you
call, not a host that registers a connector, so there is nothing to serve it to.

**Do not expose `athenaeum serve` through a stdio-to-HTTP proxy and a public tunnel**
to work around any of this. That server has no authentication: anyone who finds the
URL can read and write your entire knowledge store. Remote mode is the supported path
precisely because it does not skip the authorization server — and with no issuer
configured, `athenaeum serve --transport http` refuses a non-loopback bind outright.

## I want to use OpenAI Codex

Codex gets a static snapshot, not live MCP: a scheduled job writes a context file
Codex reads. See [Codex context file](codex.md).

## Which operating systems are supported

- **macOS** — supported.
- **Linux** — supported. The same hooks and commands work unchanged; there are no
  macOS TCC permission prompts (the "would like to access data from other apps"
  dialogs) on Linux.
- **Windows** — not supported today. The hooks are bash scripts, there is no Windows
  CI, and file locking is untested there. Support is tracked in the
  [Windows support issue](https://github.com/Kromatic-Innovation/athenaeum/issues/2203)
  — add your use case there to request it, or contribute (see
  [CONTRIBUTING.md](../../CONTRIBUTING.md)).

## See also

- Guides — [Cursor](cursor.md) · [Install](install.md) · [Passive recall via hooks](sidecar.md) · [Codex context file](codex.md) · [Troubleshooting](troubleshooting.md)
- Modules — [mcp](../modules/mcp.md) · [recall](../modules/recall.md)
