# Security Policy

## Supported versions

Athenaeum is pre-1.0 and has no long-term-support branches. Security fixes are made for the **latest minor release line** only (for example, once 0.25.0 is out, 0.24.x no longer receives fixes). Within that line, a fix ships as a new patch release of the latest version; older minor lines are not patched or backported, so upgrade to receive a fix. Fixes land on `develop` first and are promoted to `main` via the `Promote Main` workflow, and the fixed release is published to PyPI from `main`.

| Version | Security fixes |
| --- | --- |
| Latest minor release line (the newest `0.x` on PyPI) | Yes |
| Any older release | No |

## Reporting a vulnerability

Athenaeum is maintained by Kromatic Innovation. Security reports go to **open-source@kromatic.com**, which is actively monitored by the maintainers.

Please report security vulnerabilities privately via email to **open-source@kromatic.com** with the subject prefix `[athenaeum security]`. Do **not** open a public GitHub issue for security problems.

You may also use GitHub's private vulnerability reporting (Security Advisories) on the repository's Security tab.

Include:

- A description of the vulnerability and its impact
- Steps to reproduce (ideally a minimal failing test or PoC)
- Affected version or commit SHA

We aim to:

- Acknowledge your report within 3 business days
- Confirm the vulnerability (or explain why it isn't one) within 10 business days
- Ship a fix and public advisory within 30 business days of confirmation

## Scope

Athenaeum writes files to a user-controlled knowledge directory and makes authenticated calls to the Anthropic API. Security-relevant areas include:

- **Prompt injection** against the tiered LLM pipeline (Tier 2/3) and the `query-topics` preprocessor
- **Path traversal** via entity filenames or `source` parameters
- **API key handling** — we expect callers to source keys from their own secret stores; don't embed keys in configuration or logs
- **`claude-cli` subprocess backend** (`athenaeum.provider`, athenaeum#330) — with `ATHENAEUM_LLM_PROVIDER=claude-cli` the compile path shells out to the `claude` binary via an **argv list** (no shell string; page/raw content is never interpolated into a command line), relying on the operator's **ambient Claude Code login** (no API key is read, stored, or logged on this path), and runs from a **neutral working directory** so the subprocess cannot inherit a project `CLAUDE.md` / `.mcp.json`. Treat command/argument injection, argument smuggling, or credential leakage in this path as in-scope
- **MCP server input handling** — the `remember` / `recall` tools accept arbitrary text from AI agents
- **Write-time intake screening** (`athenaeum.screening`, athenaeum#320) — an opt-in, transparent keyword/regex classifier runs over every `remember()` payload BEFORE the append-only write. It is **off by default**; when enabled (`screening.medical.action: label_restrict`) it auto-stamps a read-time `access:` label (default `personal`) on matched **medical** intake so audience-scoped `recall` never surfaces it — it labels, never drops. It is deliberately not an ML/PHI model, so the classification boundary stays auditable and diff-reviewable. Unmatched intake keeps the safe default `internal` (never world-readable), so a medical false-negative is not world-readable by default. Treat a screening bypass that lets genuinely regulated content compile to a world-readable `open` page as in-scope
- **HTTP transport and OIDC resource-server auth** — `athenaeum serve --transport http` exposes the MCP tools over a network socket, `serve.auth.*` verifies OIDC bearer tokens (JWKS, issuer, audience and subject checks, RFC 9728 metadata), and the `remote` serve profile restricts the served tools and withholds excluded fields. Without an auth issuer the server refuses any non-loopback bind. Treat a token-verification bypass (wrong issuer, audience, algorithm or expired token accepted; a token with no `exp` accepted; a token with a future `nbf` or `iat` accepted), a client outside `serve.auth.allowed_clients` served (or a client the allowlist marks read-only allowed to write), `--profile remote` serving with no audience configured, a non-loopback bind without auth, or a tool or excluded field reachable outside the configured allowlist as in-scope. With an issuer configured the Host/Origin guard is off by design and bearer-token validation is the only DNS-rebinding defence, so a request that reaches a tool without a valid token is in scope. TLS termination and any reverse proxy are the operator's responsibility; see `docs/guides/remote.md`.
- **Audience-scoped recall** — `athenaeum serve --audience` filters what `recall` returns via page `access:` / `audience:` frontmatter, failing closed for untagged pages. It is a single-owner read filter for restricting secondary/scheduled agents, **not** a multi-user authentication/ACL boundary; treat a scoping bypass (a restricted audience receiving a withheld page's title, snippet, body, or ranking slot) as in-scope
- **Example hooks under `examples/claude-code/`** — the shell hooks touch `~/.cache/athenaeum/config.env` (which on 1Password-bootstrap setups contains `ANTHROPIC_API_KEY`) and inject text into Claude Code's context. Treat vulnerabilities in them as library vulnerabilities — command injection, path traversal, secret leakage, and symlink attacks are all in-scope. `config.env` is written **in plaintext**; `session-start-recall.sh` restricts it to `0600` (cache dir `0700`) on every run regardless of the file's prior mode, but this is OS-file-permission protection, not encryption — it does not protect the key from another process running as the same OS user (athenaeum#1179).

The raw intake is intentionally append-only. Attacks that rely on overwriting trusted entities are out of scope unless they bypass the append-only invariant.

## chromadb advisories

Four advisories are open against the `chromadb` version Athenaeum pins (the optional `[vector]` extra), and no chromadb release fixes them yet:

| Advisory | Severity | What it is |
| --- | --- | --- |
| GHSA-f4j7-r4q5-qw2c | critical | Code injection before authentication, through the collections endpoint |
| GHSA-36p7-vc44-83pf | critical | Code injection by an authenticated caller, through the collection update endpoint |
| GHSA-2wm9-hf6c-p5cr | high | Any authenticated user can read or change any tenant's collections |
| GHSA-xph7-9rjv-w5fr | high | The simple role provider does not check which tenant a permission applies to |

All four are flaws in chromadb's **HTTP server**. Athenaeum uses chromadb **embedded**, inside its own process (`chromadb.PersistentClient`), and does not start or connect to a chromadb server. `tests/test_chromadb_embedded_only.py` fails if anything under `src/athenaeum` uses `HttpClient`, `AsyncHttpClient`, `chromadb.server`, `chroma run` or `trust_remote_code`, so this statement stays true.

The `chromadb` package also installs a `chroma` command that can start that server. **Do not run `chroma run` (or any chromadb server) over Athenaeum's vector index directory**; doing so exposes the index to these advisories.

## Out of scope

- Issues in `[vector]` dependencies (report upstream to chromadb)
- Issues in the Anthropic SDK (report upstream to anthropic-sdk-python)
- Social-engineering attacks against maintainers or downstream adopters
