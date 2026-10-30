**Reference:** [mcp](../modules/mcp.md) · [recall](../modules/recall.md)

# Other clients and platforms

Athenaeum's MCP server (`athenaeum serve`) speaks MCP over **stdio only**, with no
network listener and no authentication. Auto-recall (context injected on every turn
without a tool call) exists only through Claude Code's hooks. Those two facts decide
what works where.

| Client | Status | What you get |
|---|---|---|
| Claude Code | Supported | All MCP tools **and** auto-recall via hooks — see [Passive recall via hooks](sidecar.md) |
| Claude Desktop, Cursor, other local (stdio) MCP clients | Works | All MCP tools, by **explicit tool call only** — no auto-recall |
| ChatGPT, Grok | Not supported | — (they need a remote HTTPS MCP server) |
| OpenAI Codex | Static snapshot | A periodically refreshed context file — see [Codex context file](codex.md) |

## I want to use Claude Code

Full support. Register the server and wire the hooks: [Install](install.md) and
[Passive recall via hooks](sidecar.md).

## I want to use Claude Desktop, Cursor, or another local MCP client

Any client that can launch a local process over stdio can run `athenaeum serve`.
The model gets every MCP tool (`remember`, `recall`, the decision-queue tools), but
it calls them only when it decides to — there is no per-turn auto-recall outside
Claude Code. Support for Cursor, including auto-recall, is tracked in the
[Cursor support issue](https://github.com/Kromatic-Innovation/athenaeum/issues/2202).

1. Install with the MCP extra: `pip install 'athenaeum[mcp]'`.
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

   Cursor and most other stdio clients take the same `mcpServers` shape (for Cursor:
   `~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project). Use absolute paths for
   both the binary and `--path`.
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

## I want to use ChatGPT or Grok

Not supported. ChatGPT and Grok connect only to a **remote MCP server over HTTPS**;
they cannot launch a local process, and athenaeum has no HTTP transport and no
authentication. Work is tracked in the
[ChatGPT epic](https://github.com/Kromatic-Innovation/athenaeum/issues/2200), the
[Grok epic](https://github.com/Kromatic-Innovation/athenaeum/issues/2201), and their
shared prerequisite, the
[remote (HTTPS) mode placeholder](https://github.com/Kromatic-Innovation/athenaeum/issues/2199).

**Do not expose `athenaeum serve` through a stdio-to-HTTP proxy and a public tunnel**
to work around this. The server has no authentication: anyone who finds the URL can
read and write your entire knowledge store.

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

- Guides — [Install](install.md) · [Passive recall via hooks](sidecar.md) · [Codex context file](codex.md) · [Troubleshooting](troubleshooting.md)
- Modules — [mcp](../modules/mcp.md) · [recall](../modules/recall.md)
